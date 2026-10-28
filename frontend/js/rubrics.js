authGuard();

/* ════════════════════════════════════════════════════════════════════════════
   DEBUG: Global submit/reload/click interceptors
   Remove once root cause is identified.
════════════════════════════════════════════════════════════════════════════ */
window.addEventListener('beforeunload', function () {
  console.error('[DEBUG] PAGE IS TRYING TO UNLOAD DURING RUBRIC/ANSWER KEY EXTRACTION');
  console.trace();
});

document.addEventListener('submit', function (e) {
  console.error('[BLOCKED] FORM SUBMIT DETECTED', e.target);
  console.trace();
  e.preventDefault();
  e.stopPropagation();
  return false;
}, true);

document.addEventListener('click', function (e) {
  const btn = e.target.closest('button');
  if (!btn) return;
  console.log('[BUTTON CLICK] id=' + btn.id + ' type=' + btn.type, btn);
}, true);

/* ════════════════════════════════════════════════════════════════════════════
   Global Extraction Banner
   ─────────────────────────────────────────────────────────────────────────
   A fixed, body-level banner that survives page reloads.

   Why body-level?
     All other loading indicators live inside #maQuestions or the section
     overlay — both of which are wiped if the page reinitialises.  This
     banner is appended directly to document.body so no page re-render can
     destroy it.

   Reload recovery:
     sessionStorage.setItem("ak_extracting_exam_<id>", "1") is written when
     extraction begins.  On every page load this module scans sessionStorage
     and, if an active extraction key is found, immediately shows the banner
     and starts polling until the rows appear.  The key is cleared only when
     rows.length > 0 is confirmed.

   Coordination with the main IIFE:
     window._akBannerShow(examId) — called from _enterUploading
     window._akBannerHide(examId) — called from _renderExtractedAnswers / _enterError
     window._rubricsUpdateModelAnswerSection(examId) — exposed by the main
       IIFE so the banner can trigger card rendering after reload recovery.
════════════════════════════════════════════════════════════════════════════ */
(function _initGlobalExtractionBanner() {
  const SK_PREFIX = 'ak_extracting_exam_';
  let _bannerEl    = null;
  let _pollId      = null;
  let _activeExamId = null;

  // ── Inject ak-spin keyframe if not already present ───────────────────────
  if (!document.getElementById('_akSpinStyle')) {
    const s = document.createElement('style');
    s.id = '_akSpinStyle';
    s.textContent = '@keyframes ak-spin{to{transform:rotate(360deg)}}';
    document.head.appendChild(s);
  }

  // ── Banner DOM ────────────────────────────────────────────────────────────
  function _ensureBanner() {
    if (_bannerEl) return _bannerEl;
    _bannerEl = document.createElement('div');
    _bannerEl.id = 'akGlobalBanner';
    Object.assign(_bannerEl.style, {
      position:    'fixed',
      top:         '0',
      left:        '0',
      right:       '0',
      zIndex:      '999999',
      background:  'linear-gradient(135deg,#28405B,#446B95)',
      color:       '#fff',
      display:     'none',
      alignItems:  'center',
      gap:         '14px',
      padding:     '13px 24px',
      boxShadow:   '0 4px 28px rgba(0,0,0,0.28)',
      fontFamily:  'Poppins,sans-serif',
      fontSize:    '14px',
      fontWeight:  '500',
      letterSpacing: '0.01em',
    });
    _bannerEl.innerHTML =
      '<div style="width:18px;height:18px;flex-shrink:0;border:2px solid rgba(255,255,255,0.3);border-top-color:#fff;border-radius:50%;animation:ak-spin .85s linear infinite;"></div>' +
      '<span style="flex:1;">Extracting answer key\u2026 &nbsp;This may take a few minutes. Please do not refresh the page.</span>' +
      '<button type="button" id="_akCancelBtn" style="' +
        'margin-left:auto;padding:5px 16px;border-radius:8px;border:1.5px solid rgba(255,255,255,0.55);' +
        'background:rgba(255,255,255,0.12);color:#fff;font-family:Poppins,sans-serif;font-size:13px;' +
        'font-weight:600;cursor:pointer;transition:background .15s;white-space:nowrap;' +
      '">Cancel Extraction</button>';
    document.body.appendChild(_bannerEl);

    // Cancel button handler
    document.getElementById('_akCancelBtn').addEventListener('click', function () {
      _cancelExtraction('Extraction cancelled.');
    });

    return _bannerEl;
  }

  function _showBanner() { const b = _ensureBanner(); b.style.display = 'flex'; }
  function _hideBanner()  { if (_bannerEl) _bannerEl.style.display = 'none'; }

  // ── Cancel / cleanup ──────────────────────────────────────────────────────
  function _cancelExtraction(reason) {
    const examId = _activeExamId;
    _stopPoll();
    if (examId) _clearKey(examId);
    _clearAllKeys();
    _hideBanner();
    _activeExamId = null;
    // Release the page-level lock so loadModelAnswersForExam can run again
    if (typeof window._akForceUnlock === 'function') window._akForceUnlock();
    console.log('[AK-BANNER] extraction cancelled — reason:', reason);
    if (reason && reason !== 'Extraction cancelled.') {
      // Show a non-blocking warning toast (use the page toast if available)
      const toastFn = (typeof toast === 'function') ? toast : null;
      if (toastFn) {
        toastFn(reason, 'warning');
      } else {
        // Fallback: ephemeral fixed label under the banner area
        const w = document.createElement('div');
        Object.assign(w.style, {
          position: 'fixed', top: '12px', left: '50%', transform: 'translateX(-50%)',
          zIndex: '999998', background: '#92400e', color: '#fff',
          padding: '8px 20px', borderRadius: '10px', fontFamily: 'Poppins,sans-serif',
          fontSize: '13px', boxShadow: '0 4px 18px rgba(0,0,0,0.22)',
        });
        w.textContent = reason;
        document.body.appendChild(w);
        setTimeout(() => w.remove(), 5000);
      }
    }
  }

  // ── sessionStorage helpers ────────────────────────────────────────────────
  function _setKey(examId)    { try { sessionStorage.setItem(SK_PREFIX + examId, '1'); } catch (_) {} }
  function _clearKey(examId)  { try { sessionStorage.removeItem(SK_PREFIX + examId); } catch (_) {} }
  function _hasKey(examId)    { try { return !!sessionStorage.getItem(SK_PREFIX + examId); } catch (_) { return false; } }

  function _clearAllKeys() {
    try {
      const keys = [];
      for (let i = 0; i < sessionStorage.length; i++) {
        const k = sessionStorage.key(i);
        if (k && k.startsWith(SK_PREFIX)) keys.push(k);
      }
      keys.forEach(k => sessionStorage.removeItem(k));
    } catch (_) {}
  }

  // ── Poll ──────────────────────────────────────────────────────────────────
  const MAX_CONSEC_404 = 3;

  function _stopPoll() { if (_pollId) { clearInterval(_pollId); _pollId = null; } }

  function _startPoll(examId) {
    _stopPoll();
    _activeExamId = examId;
    let consec404 = 0;

    _pollId = setInterval(async () => {
      try {
        const rows = await apiFetch('/model-answers/?exam_id=' + examId);
        consec404 = 0; // reset on any successful response
        if (Array.isArray(rows) && rows.length > 0) {
          console.log('[AK-BANNER] extraction complete — ' + rows.length + ' row(s) found for examId=' + examId);
          _stopPoll();
          _clearKey(examId);
          _hideBanner();
          _activeExamId = null;
          if (typeof window._akForceUnlock === 'function') window._akForceUnlock();
          // Trigger card render — works both on fresh page and after reload recovery
          if (typeof window._rubricsUpdateModelAnswerSection === 'function') {
            window._rubricsUpdateModelAnswerSection(examId);
          }
        }
        // rows.length === 0 → still processing; keep polling
      } catch (err) {
        if (err && err.status === 404) {
          consec404++;
          console.warn('[AK-BANNER] 404 for examId=' + examId + ' (consecutive: ' + consec404 + '/' + MAX_CONSEC_404 + ')');
          if (consec404 >= MAX_CONSEC_404) {
            _cancelExtraction('Extraction session expired or exam no longer exists.');
          }
        }
        // Any other error (network hiccup, 5xx) — reset counter and keep polling
      }
    }, 5000);
  }

  // ── Public API ────────────────────────────────────────────────────────────
  window._akBannerShow = function (examId) {
    _setKey(examId);
    _showBanner();
    _startPoll(examId);
    console.log('[AK-BANNER] shown for examId=' + examId);
  };

  window._akBannerHide = function (examId) {
    _clearKey(examId !== undefined ? examId : _activeExamId);
    _stopPoll();
    _hideBanner();
    _activeExamId = null;
    console.log('[AK-BANNER] hidden');
  };

  // Cancel extraction (user-triggered or programmatic)
  window._akCancelExtraction = function () { _cancelExtraction('Extraction cancelled.'); };

  // Called from fExam change handler when user switches exams during active extraction
  window._akExamChanged = function (newExamId) {
    if (_activeExamId !== null && _activeExamId !== newExamId) {
      console.log('[AK-BANNER] exam changed from ' + _activeExamId + ' → ' + newExamId + ' — auto-cancelling extraction');
      _cancelExtraction(null); // silent cancel (no toast) — user is intentionally switching
    }
  };

  // Expose the sessionStorage key check so page guards can use it
  window._akExtractionKeyActive = function (examId) { return _hasKey(examId); };

  // ── Reload recovery ───────────────────────────────────────────────────────
  function _recover() {
    try {
      const keys = [];
      for (let i = 0; i < sessionStorage.length; i++) {
        const k = sessionStorage.key(i);
        if (k && k.startsWith(SK_PREFIX)) keys.push(k);
      }
      if (!keys.length) return;

      // There should only ever be one active extraction at a time.
      // If there are multiple stale keys, clear them all and recover the first.
      const examId = parseInt(keys[0].slice(SK_PREFIX.length), 10);
      if (!examId) { _clearAllKeys(); return; }

      console.log('[AK-BANNER] reload recovery — examId=' + examId + ', showing banner and polling');
      _showBanner();
      _startPoll(examId);
    } catch (_) {}
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _recover);
  } else {
    _recover();
  }
})();

// ── Double-init guard ──────────────────────────────────────────────────────
// Prevents the IIFE below from running a second time if the script is somehow
// evaluated twice (e.g. via bfcache restore, duplicate <script> tag, or any
// dynamic re-injection).  Stored on window so the flag survives closure scope.
if (window._rubricsInitDone) {
  console.error('[RUBRICS-INIT] DOUBLE INIT DETECTED — script is being evaluated a second time. Halting. Check for duplicate <script> tags, bfcache restore, or dynamic script injection.', new Error('double-init stack trace'));
  // Intentional early exit — do not re-register event listeners or re-fetch data.
} else {

window._rubricsInitDone = true;

(async function () {
  'use strict';

  /* ── Constants ─────────────────────────────────────────────────────────── */
  const MAX_BYTES   = 50 * 1024 * 1024;  // 50 MB
  const THIS_STEP   = 2;
  const CHECK_SVG   = '<svg viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></svg>';

  const WORKFLOW_STEPS = [
    { num:1, href:'./manage-exams.html'    },
    { num:2, href:'./rubrics.html'         },
    { num:3, href:'./answer-files.html'    },
    { num:4, href:'./auto-grading.html'    },
    { num:5, href:'./results-reviews.html' },
  ];

  const DEPT_LABELS = { cs:'Computer Science', is:'Information Systems', it:'Information Technology' };

  /* ── State ─────────────────────────────────────────────────────────────── */
  let exams           = [];
  let rubrics         = [];
  let deleteId        = null;
  let selectedFile    = null;
  let currentWf       = null;
  let maCurrentExamId = null;
  let maIsSaved       = false;
  // Server-assigned IDs for the model answers currently loaded for maCurrentExamId.
  // Used on save to detect which entries were removed (need DELETE) vs kept (need PUT).
  let serverAnswerIds = new Set();
  // Set to true while answer-key extraction is in flight.
  // loadModelAnswersForExam checks this so the exam-change handler
  // cannot wipe the progress bar while extraction is active.
  let _akExtractionActive = false;
  // Drag-and-drop reorder state
  let _dragCard = null;

  /* ════════════════════════════════════════════════════════════════════════
     IndexedDB — browser-side PDF blob storage (preview only)
     File binary is never sent to the backend; only metadata is persisted there.
  ════════════════════════════════════════════════════════════════════════ */
  const IDB_DB    = 'taqdeer_db';
  const IDB_VER   = 1;
  const IDB_STORE = 'rubric_files';
  let _db = null;

  function openIDB() {
    if (_db) return Promise.resolve(_db);
    return new Promise((resolve, reject) => {
      const req = indexedDB.open(IDB_DB, IDB_VER);
      req.onupgradeneeded = function (e) {
        const db = e.target.result;
        if (!db.objectStoreNames.contains(IDB_STORE)) {
          db.createObjectStore(IDB_STORE, { keyPath: 'id' });
        }
      };
      req.onsuccess = function (e) { _db = e.target.result; resolve(_db); };
      req.onerror   = function (e) { reject(e.target.error); };
    });
  }

  async function idbSave(id, file) {
    const db = await openIDB();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(IDB_STORE, 'readwrite');
      tx.objectStore(IDB_STORE).put({ id, file });
      tx.oncomplete = () => resolve();
      tx.onerror    = e => reject(e.target.error);
    });
  }

  async function idbGet(id) {
    const db = await openIDB();
    return new Promise((resolve, reject) => {
      const tx  = db.transaction(IDB_STORE, 'readonly');
      const req = tx.objectStore(IDB_STORE).get(id);
      req.onsuccess = e => resolve(e.target.result ? e.target.result.file : null);
      req.onerror   = e => reject(e.target.error);
    });
  }

  async function idbDelete(id) {
    const db = await openIDB();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(IDB_STORE, 'readwrite');
      tx.objectStore(IDB_STORE).delete(id);
      tx.oncomplete = () => resolve();
      tx.onerror    = e => reject(e.target.error);
    });
  }

  /* ════════════════════════════════════════════════════════════════════════
     Data loaders — backend
  ════════════════════════════════════════════════════════════════════════ */
  async function loadExams() {
    console.trace('[RUBRICS-TRACE] loadExams called');
    if (_akExtractionActive) { console.warn('[RUBRICS-TRACE] loadExams BLOCKED — extraction in flight'); return; }
    try { exams = await apiFetch('/exams/'); }
    catch (e) { toast('Failed to load exams. Is the server running?', 'error'); exams = []; }
  }

  async function loadRubrics() {
    console.trace('[RUBRICS-TRACE] loadRubrics called');
    if (_akExtractionActive) { console.warn('[RUBRICS-TRACE] loadRubrics BLOCKED — extraction in flight'); return; }
    try { rubrics = await apiFetch('/rubrics/'); }
    catch (e) { toast('Failed to load rubrics.', 'error'); rubrics = []; }
  }

  /* ── Formatting helpers ─────────────────────────────────────────────── */
  function esc(s) {
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function fmtDate(iso) {
    return new Date(iso).toLocaleDateString('en-US', {
      day: 'numeric', month: 'short', year: 'numeric'
    });
  }

  function fmtSize(bytes) {
    if (bytes < 1024)        return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
  }

  /* ════════════════════════════════════════════════════════════════════════
     User chip
  ════════════════════════════════════════════════════════════════════════ */
  function initUserChip() {
    try {
      const p = JSON.parse(localStorage.getItem('taqdeer_profile') || 'null');
      if (!p) return;
      const name   = getDisplayName(p) || p.email || 'User';
      const _inits = name.split(' ').filter(Boolean).map(w => w[0]).join('').slice(0, 2).toUpperCase() || 'U';
      const _avUrl = p.avatarPath ? API_BASE + '/' + p.avatarPath : null;
      const av     = document.getElementById('chipAvatar');
      const nm     = document.getElementById('chipName');
      if (nm) nm.textContent = name;
      if (av) {
        if (_avUrl) {
          av.innerHTML = `<img src="${_avUrl}" alt="avatar" style="width:100%;height:100%;object-fit:cover;border-radius:50%;">`;
        } else {
          av.textContent = _inits;
        }
      }
    } catch (e) {}
  }

  /* ════════════════════════════════════════════════════════════════════════
     Per-exam workflow — derived from backend exam.workflowStep
     Frontend: 1=exam, 2=rubrics+answers, 3=answer-files, 4=auto-grading, 5=results
     Backend:  1=created, 2=rubric, 3=model-answers, 4=answer-files, 5=grading
  ════════════════════════════════════════════════════════════════════════ */
  function wfFromExam(examId) {
    const exam = examId ? exams.find(e => e.id === examId) : null;
    if (!exam) return { currentStep: THIS_STEP, completedSteps: [] };
    const ws = exam.workflowStep;
    const completed = [];
    if (ws >= 1) completed.push(1);  // exam exists → step 1 done
    if (ws >= 3) completed.push(2);  // rubric + answers done → step 2 done
    if (ws >= 4) completed.push(3);  // answer files done → step 3 done
    if (ws >= 5) completed.push(4);  // grading done → step 4 done
    return { currentStep: THIS_STEP, completedSteps: completed.filter(s => s !== THIS_STEP) };
  }

  /* ════════════════════════════════════════════════════════════════════════
     Workflow progress
  ════════════════════════════════════════════════════════════════════════ */
  function renderProgress(wf) {
    const completedSteps = (wf && wf.completedSteps) ? wf.completedSteps : [];
    const currentStep    = (wf && wf.currentStep)    ? wf.currentStep    : THIS_STEP;
    let   completedCount = 0;

    WORKFLOW_STEPS.forEach(function (step) {
      const el = document.getElementById('wps-' + step.num);
      if (!el) return;

      let state;
      if (completedSteps.indexOf(step.num) !== -1) { state = 'completed'; completedCount++; }
      else if (step.num === currentStep)             { state = 'active'; }
      else                                           { state = 'upcoming'; }

      el.className = 'wp-step ' + state;
      el.querySelector('.wp-bubble').innerHTML = state === 'completed' ? CHECK_SVG : step.num;
      el.onclick = null;
      if (state === 'completed') { (href => { el.onclick = () => { if (_akExtractionActive) { toast('Please wait \u2014 answer key extraction is still running.', 'info'); return; } window.location.href = href; }; })(step.href); }
      el.title = state === 'upcoming' ? 'Complete earlier steps first' : '';
    });

    const fillEl = document.getElementById('wpFill');
    if (fillEl) fillEl.style.width = (Math.min(completedCount, WORKFLOW_STEPS.length - 1) / WORKFLOW_STEPS.length * 100) + '%';

    updateNextBtn();
  }

  function updateNextBtn() {
    const btn    = document.getElementById('progressNextBtn');
    const hintEl = document.getElementById('nextHint');
    const examId = parseInt(document.getElementById('fExam').value, 10) || null;
    const hasExamRubric   = examId && rubrics.some(r => r.examId === examId);
    const hasSavedAnswers = maCurrentExamId && serverAnswerIds.size > 0;
    const hasRubricOrKey  = hasExamRubric || hasSavedAnswers;

    let hint;
    if (!examId)            hint = 'Select an exam to continue';
    else if (!hasRubricOrKey) hint = 'Upload a rubric or answer key for this exam to continue';
    else                    hint = '';

    btn.disabled = !!hint;
    btn.title    = hint || 'Proceed to Upload Answers';
    if (hintEl) { hintEl.textContent = hint; hintEl.style.display = hint ? '' : 'none'; }
  }

  document.getElementById('progressNextBtn').addEventListener('click', async function () {
    // Block navigation while answer key extraction is active — navigating away
    // would disconnect the UI from the running background process.
    if (_akExtractionActive) { toast('Please wait \u2014 answer key extraction is still running.', 'info'); return; }
    const examId = parseInt(document.getElementById('fExam').value, 10) || null;
    if (!examId) { toast('Please select an exam first.', 'info'); return; }
    const hasRubric = rubrics.some(r => r.examId === examId);
    const hasKey    = maCurrentExamId === examId && serverAnswerIds.size > 0;
    if (!hasRubric && !hasKey) { toast('Upload a rubric or answer key for this exam first.', 'info'); return; }
    window.location.href = './answer-files.html?examId=' + examId;
  });

  /* ════════════════════════════════════════════════════════════════════════
     Exam dropdown — populated from backend data
  ════════════════════════════════════════════════════════════════════════ */
  function populateExamDropdown() {
    const select = document.getElementById('fExam');
    const alert  = document.getElementById('noExamsAlert');
    const row    = document.getElementById('uploadRow');

    while (select.options.length > 1) select.remove(1);

    if (!exams.length) {
      alert.style.display     = 'flex';
      row.style.opacity       = '0.45';
      row.style.pointerEvents = 'none';
      return;
    }

    alert.style.display     = 'none';
    row.style.opacity       = '';
    row.style.pointerEvents = '';

    const groups = {};
    exams.forEach(e => {
      const d = (e.dept || 'other').toLowerCase();
      if (!groups[d]) groups[d] = [];
      groups[d].push(e);
    });

    Object.keys(groups).sort().forEach(dept => {
      const og   = document.createElement('optgroup');
      og.label   = DEPT_LABELS[dept] || dept.toUpperCase();
      groups[dept].forEach(e => {
        const opt       = document.createElement('option');
        opt.value       = e.id;
        opt.textContent = e.name + ' \u2014 ' + e.section;
        og.appendChild(opt);
      });
      select.appendChild(og);
    });
  }

  /* ════════════════════════════════════════════════════════════════════════
     Form error helpers
  ════════════════════════════════════════════════════════════════════════ */
  function showErr(id, msg) {
    const el = document.getElementById('e-' + id);
    if (!el) return;
    el.textContent = msg;
    el.classList.add('show');
  }
  function clearErr(id) {
    const el = document.getElementById('e-' + id);
    if (el) el.classList.remove('show');
  }

  /* ════════════════════════════════════════════════════════════════════════
     Custom file picker
  ════════════════════════════════════════════════════════════════════════ */
  const filePickBtn = document.getElementById('filePickBtn');
  const fileInput   = document.getElementById('fRubricFile');
  const fpName      = document.getElementById('fpName');

  filePickBtn.addEventListener('click', () => {
    fileInput.click();
  });
  filePickBtn.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fileInput.click(); } });

  fileInput.addEventListener('change', function () {
    const file  = fileInput.files[0];
    if (!file) return;
    const isPDF = file.type === 'application/pdf' || file.name.toLowerCase().endsWith('.pdf');
    if (!isPDF) {
      showErr('fRubricFile', 'Only PDF files are accepted.');
      filePickBtn.classList.add('invalid');
      fileInput.value = ''; selectedFile = null;
      filePickBtn.classList.remove('has-file');
      fpName.textContent = 'Choose PDF file\u2026';
      return;
    }
    if (file.size > MAX_BYTES) {
      showErr('fRubricFile', 'File size must not exceed 50 MB.');
      filePickBtn.classList.add('invalid');
      fileInput.value = ''; selectedFile = null;
      filePickBtn.classList.remove('has-file');
      fpName.textContent = 'Choose PDF file\u2026';
      return;
    }
    selectedFile = file;
    filePickBtn.classList.add('has-file');
    filePickBtn.classList.remove('invalid');
    fpName.textContent = file.name + ' (' + fmtSize(file.size) + ')';
    clearErr('fRubricFile');
  });

  /* ════════════════════════════════════════════════════════════════════════
     Upload handler — POST /rubrics/ then store blob in IndexedDB
  ════════════════════════════════════════════════════════════════════════ */
  document.getElementById('btnUpload').addEventListener('click', async function () {
    // If no file is selected yet, open the file picker instead of showing an error.
    if (!selectedFile) {
      fileInput.click();
      return;
    }
    await _doRubricUpload();
  });

  async function _doRubricUpload() {
    const sel    = document.getElementById('fExam');
    const examId = parseInt(sel.value, 10);
    let   valid  = true;

    if (!examId) {
      showErr('fExam', 'Please select an exam.'); sel.classList.add('invalid'); valid = false;
    } else { clearErr('fExam'); sel.classList.remove('invalid'); }

    // File is guaranteed to be selected here (Upload button handler checks first).
    clearErr('fRubricFile'); filePickBtn.classList.remove('invalid');

    if (!valid) return;

    const btn     = document.getElementById('btnUpload');
    const txtEl   = document.getElementById('uploadText');
    const spinner = document.getElementById('uploadSpinner');
    btn.disabled = true; txtEl.textContent = 'Uploading\u2026'; spinner.style.display = 'inline-block';

    try {
      // 1 — POST metadata to backend; backend assigns the canonical ID
      const rubric = await apiFetch('/rubrics/', {
        method: 'POST',
        body:   JSON.stringify({ examId, fileName: selectedFile.name, fileSize: selectedFile.size }),
      });

      // 2 — Store blob in IndexedDB keyed by server ID (for preview)
      await idbSave(rubric.id, selectedFile);

      // 3 — Upsert in local array (backend guarantees one rubric per exam;
      //     if an existing entry was replaced in-place, its ID stays the same)
      const capturedExamId = examId;
      const existingIdx = rubrics.findIndex(r => r.examId === capturedExamId);
      if (existingIdx !== -1) {
        rubrics[existingIdx] = rubric;
      } else {
        rubrics.push(rubric);
      }
      sel.value = ''; selectedFile = null; fileInput.value = '';
      filePickBtn.classList.remove('has-file', 'invalid');
      fpName.textContent = 'Choose PDF file\u2026';

      // 4 — Refresh UI
      renderTable();
      renderProgress(null);
      updateRubricNotice(currentWf);         // refresh notice + button text
      await updateModelAnswerSection(capturedExamId);
      toast('Rubric uploaded successfully.', 'success');

    } catch (err) {
      toast('Upload failed \u2014 ' + err.message, 'error');
    } finally {
      btn.disabled = false; txtEl.textContent = 'Upload'; spinner.style.display = 'none';
    }
  }

  /* ════════════════════════════════════════════════════════════════════════
     Rubric notice — simple informational note only
  ════════════════════════════════════════════════════════════════════════ */
  function updateRubricNotice(wf) {
    const uploadText = document.getElementById('uploadText');
    const examId     = parseInt(document.getElementById('fExam').value, 10);

    if (!examId) {
      if (uploadText) uploadText.textContent = 'Upload';
      return;
    }

    const hasRubric = rubrics.some(r => r.examId === examId);
    if (uploadText) uploadText.textContent = hasRubric ? 'Replace Rubric' : 'Upload';
  }

  /* ════════════════════════════════════════════════════════════════════════
     Table render
  ════════════════════════════════════════════════════════════════════════ */
  function renderTable() {
    const tbody    = document.getElementById('rubricsBody');
    const tableWrp = document.getElementById('rubricsTableWrap');
    const emptyDiv = document.getElementById('rubricsEmpty');
    const countEl  = document.getElementById('rubricCount');

    if (!rubrics.length) {
      tableWrp.style.display = 'none'; emptyDiv.style.display = 'block'; countEl.style.display = 'none';
      return;
    }

    tableWrp.style.display = 'block'; emptyDiv.style.display = 'none';
    countEl.style.display  = 'inline-flex';
    countEl.textContent    = rubrics.length + (rubrics.length === 1 ? ' rubric' : ' rubrics');

    tbody.innerHTML = rubrics.map((r, i) => {
      const exam       = exams.find(e => e.id === r.examId);
      const examName   = exam ? esc(exam.name)    : null;
      const courseCode = exam ? esc(exam.section) : null;

      const examCell   = examName
        ? '<span class="exam-lbl" title="' + examName + '">' + examName + '</span>'
        : '<span class="exam-gone">Deleted Exam</span>';
      const courseCell = courseCode
        ? '<span class="course-chip">' + courseCode + '</span>'
        : '<span style="color:var(--text-muted);">\u2014</span>';

      return '<tr style="animation:fadeUp .3s ease both;animation-delay:' + (i * 0.045) + 's;">'
        + '<td><div class="file-cell">'
        +   '<div class="pdf-badge"><svg viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="9" y1="13" x2="15" y2="13"/><line x1="9" y1="17" x2="15" y2="17"/></svg></div>'
        +   '<div><div class="file-lbl" title="' + esc(r.fileName) + '">' + esc(r.fileName) + '</div>'
        +       '<div class="size-lbl">' + fmtSize(r.fileSize) + '</div></div>'
        + '</div></td>'
        + '<td>' + examCell + '</td>'
        + '<td>' + courseCell + '</td>'
        + '<td class="date-lbl">' + fmtDate(r.uploadedAt) + '</td>'
        + '<td><div class="tbl-actions">'
        +   '<button class="act-btn view" title="Preview PDF" onclick="viewRubric(' + r.id + ')">'
        +     '<svg viewBox="0 0 24 24"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>'
        +   '</button>'
        +   '<button class="btn-del" onclick="openDelete(' + r.id + ')">Delete</button>'
        + '</div></td>'
        + '</tr>';
    }).join('');
  }

  /* ════════════════════════════════════════════════════════════════════════
     View / preview rubric PDF (blob from IndexedDB)
  ════════════════════════════════════════════════════════════════════════ */
  window.viewRubric = async function (id) {
    try {
      const file = await idbGet(id);
      if (!file) { toast('File data not available \u2014 please re-upload the rubric.', 'error'); return; }
      const url = URL.createObjectURL(file);
      window.open(url, '_blank');
      setTimeout(() => URL.revokeObjectURL(url), 15000);
    } catch (e) { toast('Could not open file. Please try again.', 'error'); }
  };

  /* ════════════════════════════════════════════════════════════════════════
     Delete flow — DELETE /rubrics/{id}
  ════════════════════════════════════════════════════════════════════════ */
  window.openDelete = function (id) {
    const r = rubrics.find(r => r.id === id);
    if (!r) return;

    deleteId = id;
    document.getElementById('delFileName').textContent = r.fileName;
    document.getElementById('deleteModal').classList.add('open');
  };

  document.getElementById('delCancelBtn').addEventListener('click', () => {
    document.getElementById('deleteModal').classList.remove('open');
    deleteId = null;
  });
  document.getElementById('deleteModal').addEventListener('click', e => {
    if (e.target === e.currentTarget) { document.getElementById('deleteModal').classList.remove('open'); deleteId = null; }
  });

  document.getElementById('delConfirmBtn').addEventListener('click', async function () {
    if (deleteId === null) return;
    const r    = rubrics.find(r => r.id === deleteId);
    const name = r ? r.fileName : 'Rubric';
    const btn  = this;
    btn.disabled = true;

    try {
      await apiFetch('/rubrics/' + deleteId, { method: 'DELETE' });
      rubrics = rubrics.filter(r => r.id !== deleteId);
      try { await idbDelete(deleteId); } catch (e) { /* silent — metadata already removed */ }

      deleteId = null;
      document.getElementById('deleteModal').classList.remove('open');
      renderTable();
      renderProgress(null);
      updateRubricNotice(currentWf);         // revert button text / notice
      const currentSel = parseInt(document.getElementById('fExam').value, 10) || null;
      await updateModelAnswerSection(currentSel);
      toast('\u201c' + name + '\u201d deleted.', 'info');

    } catch (err) {
      toast('Delete failed \u2014 ' + err.message, 'error');
    } finally {
      btn.disabled = false;
    }
  });

  async function _doRubricDelete(id, fileName) {
    try {
      await apiFetch('/rubrics/' + id, { method: 'DELETE' });
      rubrics = rubrics.filter(r => r.id !== id);
      try { await idbDelete(id); } catch (e) { /* silent */ }
      renderTable();
      renderProgress(null);
      updateRubricNotice(currentWf);
      const currentSel = parseInt(document.getElementById('fExam').value, 10) || null;
      await updateModelAnswerSection(currentSel);
      toast('\u201c' + fileName + '\u201d deleted.', 'info');
    } catch (err) {
      toast('Delete failed \u2014 ' + err.message, 'error');
    }
  }

  /* ════════════════════════════════════════════════════════════════════════
     Grading configuration constants
  ════════════════════════════════════════════════════════════════════════ */
  const SCORING_LABELS = {
    keyword_matching:                'Keyword / Concept Matching',
    semantic_similarity:             'Semantic Similarity',
    holistic_judgment:               'Holistic Judgment',
    method_result_weighted:          'Method + Result Weighted',
    answer_only:                     'Answer Only',
    step_by_step_rubric:             'Step-by-Step Rubric',
    element_checklist:               'Element Checklist',
    structural_correctness:          'Structural Correctness',
    exact_match:                     'Exact Match',
    partial_credit:                  'Partial Credit — proportional per correct option',
    partial_credit_with_penalty:     'Partial Credit with Penalty — deduct for incorrect selections',
  };

  // Scoring options per question type (first entry = default)
  const SCORING_BY_TYPE = {
    recall_general:        ['keyword_matching', 'semantic_similarity', 'holistic_judgment'],
    recall_differentiation:['keyword_matching', 'semantic_similarity', 'holistic_judgment'],
    mathematical:          ['method_result_weighted', 'answer_only', 'step_by_step_rubric'],
    diagrammatic_aoa:      ['element_checklist', 'structural_correctness', 'holistic_judgment'],
    multiple_choice:        ['exact_match', 'partial_credit', 'partial_credit_with_penalty'],
    multiple_choice_single: ['exact_match'],
    true_false:             ['exact_match'],
    other:                 ['keyword_matching', 'semantic_similarity', 'holistic_judgment'],
  };

  // Default scoring method per type (first scoring option)
  const DEFAULT_SCORING = {
    recall_general:        'keyword_matching',
    recall_differentiation:'keyword_matching',
    mathematical:          'method_result_weighted',
    diagrammatic_aoa:      'element_checklist',
    multiple_choice:        'partial_credit',
    multiple_choice_single: 'exact_match',
    true_false:             'exact_match',
    other:                 'keyword_matching',
  };

  // Checks per type — { val, label, def } where def = checked by default
  // Each type shows ONLY its own checks — no cross-type leakage.
  const CHECKS_BY_TYPE = {
    recall_general: [
      { val: 'accept_synonyms',              label: 'Accept synonyms and paraphrased answers',     def: true  },
      { val: 'partial_credit_key_concepts',  label: 'Partial credit for each key concept',         def: true  },
      { val: 'ignore_spelling_meaning_clear',label: 'Ignore spelling errors if meaning is clear',  def: false },
      { val: 'require_concrete_example',     label: 'Require at least one concrete example',       def: false },
    ],
    recall_differentiation: [
      { val: 'accept_synonyms',              label: 'Accept synonyms and paraphrases',                    def: true  },
      { val: 'ignore_spelling_meaning_clear',label: 'Ignore spelling errors if meaning is clear',         def: false },
      { val: 'require_both_sides',           label: 'Both sides required for full marks',                 def: true  },
      { val: 'require_concrete_example',     label: 'Require at least one concrete example',              def: false },
    ],
    mathematical: [
      { val: 'partial_credit_formula',  label: 'Partial credit for correct formula setup',          def: true  },
      { val: 'accept_equivalent_forms', label: 'Accept equivalent forms (fractions / decimals)',    def: true  },
      { val: 'require_units',           label: 'Require units in the final answer',                 def: false },
      { val: 'error_carried_forward',   label: 'Error carried forward from earlier steps',          def: false },
      { val: 'check_sign_conventions',  label: 'Check sign conventions (positive / negative)',      def: false },
    ],
    diagrammatic_aoa: [
      { val: 'nodes_present',            label: 'All required nodes present',                def: true  },
      { val: 'edges_correct',            label: 'All edges correct with durations',           def: true  },
      { val: 'activity_labels',          label: 'Activity labels on each edge',               def: true  },
      { val: 'arrow_directions_correct', label: 'Arrow directions must be correct',           def: false },
      { val: 'accept_alternate_layout',  label: 'Accept alternate layout orientations',       def: false },
    ],
    multiple_choice:        [],
    multiple_choice_single: [],
    true_false:             [],
    other: [
      { val: 'accept_synonyms',             label: 'Accept synonyms and paraphrased answers', def: true  },
      { val: 'partial_credit_key_concepts', label: 'Partial credit for key concepts',         def: true  },
      { val: 'ignore_spelling_meaning_clear',label: 'Ignore spelling errors if meaning is clear', def: false },
    ],
  };

  /* ════════════════════════════════════════════════════════════════════════
     Answer Key — image upload + symbol toolbar helpers
  ════════════════════════════════════════════════════════════════════════ */

  const _SYM_GROUPS = [
    { label: 'Greek',     syms: ['α','β','γ','δ','ε','θ','λ','μ','π','σ','φ','ω','Σ','Δ','Π','Ω'] },
    { label: 'Math',      syms: ['×','÷','±','√','∛','²','³','½','¼','¾','∞','∑','∏','∫','∂'] },
    { label: 'Relations', syms: ['≠','≤','≥','≈','≡'] },
    { label: 'Arrows',    syms: ['→','←','↔','⇒','⇐','⇔'] },
    { label: 'Sets',      syms: ['∈','∉','⊂','⊃','∪','∩','∅','∀','∃'] },
    { label: 'Logic',     syms: ['¬','∧','∨'] },
    { label: 'Misc',      syms: ['°','′','″'] },
  ];

  function _compressImage(file, cb) {
    const reader = new FileReader();
    reader.onload = function(e) {
      const img = new Image();
      img.onload = function() {
        const MAX = 800;
        let w = img.naturalWidth, h = img.naturalHeight;
        if (w > MAX || h > MAX) {
          if (w >= h) { h = Math.round(h * MAX / w); w = MAX; }
          else        { w = Math.round(w * MAX / h); h = MAX; }
        }
        const canvas = document.createElement('canvas');
        canvas.width = w; canvas.height = h;
        canvas.getContext('2d').drawImage(img, 0, 0, w, h);
        cb(canvas.toDataURL('image/jpeg', 0.75));
      };
      img.src = e.target.result;
    };
    reader.readAsDataURL(file);
  }

  function _wireSymbols(toggleBtn, toolbar, targetField) {
    toggleBtn.addEventListener('click', function() {
      toolbar.classList.toggle('open');
    });
    toolbar.querySelectorAll('.ma-sym-btn').forEach(btn => {
      btn.addEventListener('mousedown', function(e) {
        e.preventDefault(); // don't steal focus
        const sym = this.dataset.sym;
        const start = targetField.selectionStart;
        const end   = targetField.selectionEnd;
        const val   = targetField.value;
        targetField.value = val.slice(0, start) + sym + val.slice(end);
        targetField.selectionStart = targetField.selectionEnd = start + sym.length;
        targetField.focus();
        targetField.dispatchEvent(new Event('input'));
      });
    });
  }

  function _buildSymToolbarHTML(id) {
    let html = '';
    _SYM_GROUPS.forEach((grp, gi) => {
      if (gi > 0) html += '<div class="ma-sym-sep"></div>';
      grp.syms.forEach(s => {
        html += '<button type="button" class="ma-sym-btn" data-sym="' + s + '" title="' + s + '">' + s + '</button>';
      });
    });
    return html;
  }

  function _renderImgPreview(previewWrap, hiddenData, fileInput) {
    previewWrap.innerHTML = '';
    if (!hiddenData.value) return;
    const wrap = document.createElement('div');
    wrap.className = 'ma-img-preview-wrap';
    const img = document.createElement('img');
    img.className = 'ma-img-preview'; img.src = hiddenData.value;
    const rm = document.createElement('button');
    rm.type = 'button'; rm.className = 'ma-img-remove'; rm.title = 'Remove image';
    rm.textContent = '×';
    rm.addEventListener('click', function() {
      hiddenData.value = '';
      previewWrap.innerHTML = '';
      if (fileInput) fileInput.value = '';
      maIsSaved = false; hideMaSavedBadge(); updateNextBtn();
    });
    wrap.appendChild(img); wrap.appendChild(rm);
    previewWrap.appendChild(wrap);
  }

  function _wireImageUpload(card, fieldName) {
    const previewWrap = card.querySelector('[data-img-preview="' + fieldName + '"]');
    const hiddenData  = card.querySelector('[data-field="' + fieldName + '-img"]');
    const fileInput   = card.querySelector('[data-img-input="' + fieldName + '"]');
    const uploadBtn   = card.querySelector('[data-img-btn="' + fieldName + '"]');
    if (!previewWrap || !hiddenData || !fileInput || !uploadBtn) return;

    uploadBtn.addEventListener('click', () => fileInput.click());
    fileInput.addEventListener('change', function() {
      const file = this.files[0];
      if (!file) return;
      _compressImage(file, function(dataUrl) {
        hiddenData.value = dataUrl;
        _renderImgPreview(previewWrap, hiddenData, fileInput);
        maIsSaved = false; hideMaSavedBadge(); updateNextBtn();
      });
    });

    // Show preview if data already loaded (e.g. after restore)
    if (hiddenData.value) _renderImgPreview(previewWrap, hiddenData, fileInput);
  }

  function _persistCardImages(card, examId, answerId) {
    const key = 'taqdeer_img_' + examId + '_' + answerId;
    const qImg = card.querySelector('[data-field="question-img"]');
    const aImg = card.querySelector('[data-field="answer-img"]');
    const data = { q: qImg ? qImg.value : '', a: aImg ? aImg.value : '' };
    if (data.q || data.a) {
      localStorage.setItem(key, JSON.stringify(data));
    } else {
      localStorage.removeItem(key);
    }
  }

  function _restoreCardImages(card, examId, answerId) {
    const key = 'taqdeer_img_' + examId + '_' + answerId;
    const raw = localStorage.getItem(key);
    if (!raw) return;
    try {
      const data = JSON.parse(raw);
      ['question', 'answer'].forEach(function(f) {
        const imgVal  = f === 'question' ? data.q : data.a;
        if (!imgVal) return;
        const hidden  = card.querySelector('[data-field="' + f + '-img"]');
        const preview = card.querySelector('[data-img-preview="' + f + '"]');
        if (hidden && preview) {
          hidden.value = imgVal;
          _renderImgPreview(preview, hidden, card.querySelector('[data-img-input="' + f + '"]'));
        }
      });
    } catch(e) { /* ignore corrupted data */ }
  }

  /* ════════════════════════════════════════════════════════════════════════
     Multiple Choice / True-False structured answer helpers
  ════════════════════════════════════════════════════════════════════════ */

  /** Build a Multiple Choice options editor (returns element, initially hidden). */
  function _buildMcEditor(initData) {
    const allowMultiple = !!(initData && initData.allow_multiple);
    const initOpts = (initData && Array.isArray(initData.options) && initData.options.length)
      ? initData.options
      : [{ text: '', correct: false }, { text: '', correct: false },
         { text: '', correct: false }, { text: '', correct: false }];

    const editor = document.createElement('div');
    editor.className = 'ma-mc-editor';
    editor.style.display = 'none';

    // ── Single / multiple selector ───────────────────────────────────────────
    const allowRow = document.createElement('div');
    allowRow.className = 'ma-mc-allow-row';
    allowRow.innerHTML =
      '<label class="field-label">Correct answer mode</label>'
      + '<select class="ma-mc-allow-sel">'
      + '<option value="single"'   + (!allowMultiple ? ' selected' : '') + '>Single correct answer</option>'
      + '<option value="multiple"' + ( allowMultiple ? ' selected' : '') + '>Multiple correct answers</option>'
      + '</select>';
    editor.appendChild(allowRow);

    // ── Options list ─────────────────────────────────────────────────────────
    const optsWrap = document.createElement('div');
    optsWrap.className = 'ma-mc-options';
    editor.appendChild(optsWrap);

    const addBtn = document.createElement('button');
    addBtn.type = 'button'; addBtn.className = 'ma-mc-add-btn';
    addBtn.textContent = '+ Add option';
    editor.appendChild(addBtn);

    const errEl = document.createElement('div');
    errEl.className = 'ma-mc-err';
    errEl.textContent = 'Mark at least one correct answer.';
    editor.appendChild(errEl);

    const allowSel = allowRow.querySelector('.ma-mc-allow-sel');

    function _buildOptRow(text, correct) {
      const row = document.createElement('div');
      row.className = 'ma-mc-option' + (correct ? ' correct' : '');
      row.innerHTML =
        '<button type="button" class="ma-mc-opt-sel"></button>'
        + '<input type="text" class="ma-mc-opt-text" placeholder="Option text\u2026" value="' + esc(text) + '">'
        + '<span class="ma-mc-correct-badge">Correct answer</span>'
        + '<button type="button" class="ma-mc-opt-del" title="Remove option">&times;</button>';

      const textIn = row.querySelector('.ma-mc-opt-text');
      const delBtn = row.querySelector('.ma-mc-opt-del');

      function _toggle() {
        if (row.classList.contains('correct')) {
          row.classList.remove('correct');
        } else {
          if (allowSel.value === 'single') {
            optsWrap.querySelectorAll('.ma-mc-option').forEach(r => r.classList.remove('correct'));
          }
          row.classList.add('correct');
        }
        maIsSaved = false; hideMaSavedBadge(); updateNextBtn();
      }

      // Click anywhere on the row (except the text input or delete btn) toggles correct
      row.addEventListener('click', e => { if (e.target !== textIn && e.target !== delBtn) _toggle(); });
      textIn.addEventListener('input', () => { maIsSaved = false; hideMaSavedBadge(); updateNextBtn(); });
      delBtn.addEventListener('click', () => { row.remove(); maIsSaved = false; hideMaSavedBadge(); updateNextBtn(); });
      return row;
    }

    initOpts.forEach(o => optsWrap.appendChild(_buildOptRow(o.text, !!o.correct)));
    addBtn.addEventListener('click', () => {
      optsWrap.appendChild(_buildOptRow('', false));
      maIsSaved = false; hideMaSavedBadge(); updateNextBtn();
    });

    // When switching to single: keep only the first currently-correct option
    allowSel.addEventListener('change', () => {
      if (allowSel.value === 'single') {
        let kept = false;
        optsWrap.querySelectorAll('.ma-mc-option.correct').forEach(r => {
          if (kept) r.classList.remove('correct'); else kept = true;
        });
      }
      maIsSaved = false; hideMaSavedBadge(); updateNextBtn();
    });

    return editor;
  }

  /** Build a True / False editor (returns element, initially hidden). */
  function _buildTfEditor(initData) {
    // initData: { correct: true | false | null }
    const correctVal = (initData && initData.correct != null) ? initData.correct : null;

    const editor = document.createElement('div');
    editor.className = 'ma-tf-editor';
    editor.style.display = 'none';

    [{ label: 'True', val: true }, { label: 'False', val: false }].forEach(({ label, val }) => {
      const opt = document.createElement('div');
      opt.className = 'ma-tf-option' + ((correctVal === val) ? ' correct' : '');
      opt.dataset.tfValue = String(val);
      opt.innerHTML =
        '<button type="button" class="ma-mc-opt-sel"></button>'
        + '<span class="ma-tf-opt-label">' + label + '</span>'
        + '<span class="ma-mc-correct-badge">Correct answer</span>';
      opt.addEventListener('click', () => {
        editor.querySelectorAll('.ma-tf-option').forEach(o => o.classList.remove('correct'));
        opt.classList.add('correct');
        maIsSaved = false; hideMaSavedBadge(); updateNextBtn();
      });
      editor.appendChild(opt);
    });

    const errEl = document.createElement('div');
    errEl.className = 'ma-mc-err';
    errEl.textContent = 'Select the correct answer.';
    editor.appendChild(errEl);

    return editor;
  }

  /**
   * Show the right answer section inside a container.
   * Container must have direct children: .ma-sa-wrap, .ma-mc-editor, .ma-tf-editor
   */
  function _switchAnswerMode(container, type) {
    container.dataset.ansMode = type;
    const sa = container.querySelector(':scope > .ma-sa-wrap');
    const mc = container.querySelector(':scope > .ma-mc-editor');
    const tf = container.querySelector(':scope > .ma-tf-editor');
    const structured = (type === 'multiple_choice' || type === 'true_false');
    if (sa) sa.style.display = structured ? 'none' : '';
    if (mc) mc.style.display = (type === 'multiple_choice') ? '' : 'none';
    if (tf) tf.style.display = (type === 'true_false')      ? '' : 'none';
  }

  /** Collect the serialized answer key from an answer container (MC/TF only). */
  function _collectAnswerKey(container) {
    const mode = container.dataset.ansMode || 'short_answer';
    if (mode === 'multiple_choice') {
      const ed       = container.querySelector('.ma-mc-editor');
      const allowSel = ed && ed.querySelector('.ma-mc-allow-sel');
      const options  = [...(ed ? ed.querySelectorAll('.ma-mc-option') : [])].map(r => ({
        text:    r.querySelector('.ma-mc-opt-text')?.value.trim() || '',
        correct: r.classList.contains('correct'),
      }));
      return JSON.stringify({ type: 'multiple_choice', allow_multiple: allowSel?.value === 'multiple', options });
    }
    if (mode === 'true_false') {
      const ed  = container.querySelector('.ma-tf-editor');
      const hit = ed && ed.querySelector('.ma-tf-option.correct');
      return JSON.stringify({ type: 'true_false', correct: hit ? hit.dataset.tfValue === 'true' : null });
    }
    return null; // short_answer: caller reads textarea directly
  }

  /** Validate the answer container (MC/TF modes); marks error and returns bool. */
  function _validateAnswerContainer(container) {
    const mode = container.dataset.ansMode || 'short_answer';
    if (mode === 'multiple_choice') {
      const ed  = container.querySelector('.ma-mc-editor');
      const err = ed && ed.querySelector('.ma-mc-err');
      const ok  = !!(ed && ed.querySelector('.ma-mc-option.correct'));
      if (err) err.classList.toggle('show', !ok);
      return ok;
    }
    if (mode === 'true_false') {
      const ed  = container.querySelector('.ma-tf-editor');
      const err = ed && ed.querySelector('.ma-mc-err');
      const ok  = !!(ed && ed.querySelector('.ma-tf-option.correct'));
      if (err) err.classList.toggle('show', !ok);
      return ok;
    }
    return true;
  }

  /* ════════════════════════════════════════════════════════════════════════
     Answer Key — build question card
  ════════════════════════════════════════════════════════════════════════ */
  function buildQuestionCard(num, data) {
    data = data || {};
    const card       = document.createElement('div');
    card.className   = 'ma-question-card';
    card.dataset.num = num;
    // data.answerId is set when loading from backend so save knows whether to PUT or POST
    if (data.answerId) card.dataset.answerId = data.answerId;

    // ── Grouped mode: question has sub-questions (a, b, c…) ──────────────────
    const subQs = Array.isArray(data.sub_questions) && data.sub_questions.length > 0
      ? data.sub_questions : null;
    if (subQs) card.dataset.grouped = 'true';

    const initType    = data.questionType  || 'recall_general';
    const typeChecks  = CHECKS_BY_TYPE[initType] || CHECKS_BY_TYPE.other;
    const initScoring = data.scoringMethod || DEFAULT_SCORING[initType] || 'keyword_matching';
    const initChecks  = Array.isArray(data.checks)
      ? data.checks
      : typeChecks.filter(c => c.def).map(c => c.val);
    const initCustom  = data.customInstruction || '';
    const scoreVal    = (data.score !== undefined && data.score !== null) ? data.score : '';

    const _symToolbarHTML = _buildSymToolbarHTML();

    // ── Answer area: .ma-answer-single always rendered; CSS + [data-grouped] toggles visibility ──
    // Inside it: .ma-sa-wrap (short answer) + MC/TF editors appended programmatically
    const plainModelAnswer = (initType === 'multiple_choice' || initType === 'true_false') ? '' : (data.modelAnswer || '');
    const answerAreaHTML =
      '<div class="ma-answer-single">'
      + '<div class="ma-sa-wrap">'   // short-answer wrapper (hidden when MC/TF)
      + '<div class="ma-field">'
      + '<label class="field-label">Model answer <span class="req">*</span></label>'
      + '<textarea class="ma-textarea" data-field="answer"'
      + ' placeholder="Write the correct model answer\u2026" rows="4">'
      + esc(plainModelAnswer)
      + '</textarea>'
      + '<div class="ma-field-err">Model answer is required.</div>'
      + '<div class="ma-img-row">'
      +   '<button type="button" class="ma-img-btn" data-img-btn="answer" title="Attach image">'
      +     '<svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>'
      +     'Add image'
      +   '</button>'
      +   '<input type="file" accept="image/*" style="display:none" data-img-input="answer">'
      +   '<input type="hidden" data-field="answer-img">'
      +   '<button type="button" class="ma-sym-toggle" data-sym-toggle="answer">\u03a9 Symbols</button>'
      + '</div>'
      + '<div data-img-preview="answer"></div>'
      + '<div class="ma-sym-toolbar" data-sym-toolbar="answer">' + _symToolbarHTML + '</div>'
      + '</div>'
      + '</div>'  // .ma-sa-wrap
      // MC/TF editors are appended as siblings programmatically after innerHTML is set
      + '</div>'  // .ma-answer-single
      // Sub-question blocks container (grouped) — populated below after innerHTML is set
      + '<div class="ma-sub-questions-wrap">'
      + '<div class="ma-sub-score-warn" id="subScoreWarn_' + num + '">'
      +   '<svg viewBox="0 0 24 24"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>'
      +   'Sub-question marks exceed the main question total.'
      + '</div>'
      + '</div>';

    card.draggable = false; // enabled only when dragging from the handle
    card.innerHTML =
      // ── 1. Question header: drag handle + label + delete button ────────────
      '<div class="ma-q-header">'
      + '<div class="ma-drag-handle" title="Drag to reorder">'
      +   '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="9" cy="6" r="1" fill="currentColor" stroke="none"/><circle cx="15" cy="6" r="1" fill="currentColor" stroke="none"/><circle cx="9" cy="12" r="1" fill="currentColor" stroke="none"/><circle cx="15" cy="12" r="1" fill="currentColor" stroke="none"/><circle cx="9" cy="18" r="1" fill="currentColor" stroke="none"/><circle cx="15" cy="18" r="1" fill="currentColor" stroke="none"/></svg>'
      + '</div>'
      + '<span class="ma-q-label">Question ' + num + '</span>'
      + '<div class="ma-q-marks">'
      +   '<label class="field-label">Marks</label>'
      +   '<input type="number" class="ma-score-inline-input" data-field="score"'
      +   ' placeholder="0" min="0" max="999" step="any" value="' + scoreVal + '">'
      +   '<div class="ma-field-err ma-score-err">Max 3 digits.</div>'
      + '</div>'
      + '<button class="ma-q-del" type="button" title="Remove question"'
      + ' onclick="removeModelQuestion(this)">&times;</button>'
      + '</div>'
      // Question text (editable, part of the header region)
      + '<div class="ma-field" style="margin-bottom:10px;">'
      + '<input type="text" class="ma-input field-input" data-field="question"'
      + ' placeholder="Enter the exam question text\u2026"'
      + ' value="' + esc(data.questionText || '') + '" autocomplete="off">'
      + '<div class="ma-field-err">Question text is required.</div>'
      // Controls row: Add sub-question | separator | Add image | Symbols
      + '<div class="ma-img-row">'
      +   '<button type="button" class="btn-add-sub-q">'
      +     '<svg viewBox="0 0 24 24"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>'
      +     'Add sub-question'
      +   '</button>'
      +   '<span class="ma-toolbar-sep"></span>'
      +   '<button type="button" class="ma-img-btn" data-img-btn="question" title="Attach image">'
      +     '<svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>'
      +     'Add image'
      +   '</button>'
      +   '<input type="file" accept="image/*" style="display:none" data-img-input="question">'
      +   '<input type="hidden" data-field="question-img">'
      +   '<button type="button" class="ma-sym-toggle" data-sym-toggle="question">\u03a9 Symbols</button>'
      + '</div>'
      + '<div data-img-preview="question"></div>'
      + '<div class="ma-sym-toolbar" data-sym-toolbar="question">' + _symToolbarHTML + '</div>'
      + '</div>'
      + '<div class="ma-grading-divider"></div>'
      // ── 2. Answer type + Marks — same row ─────────────────────────────────
      + '<div class="ma-type-marks-row">'
      +   '<div class="ma-type-col">'
      +     '<label class="field-label">Answer type</label>'
      +     '<select class="qg-select" data-gp="type">'
      +       '<option value="recall_general"'         + (initType==='recall_general'         ?' selected':'') + '>Recall &amp; Short Answer \u2014 General</option>'
      +       '<option value="recall_differentiation"' + (initType==='recall_differentiation' ?' selected':'') + '>Recall &amp; Short Answer \u2014 Differentiation</option>'
      +       '<option value="mathematical"'           + (initType==='mathematical'           ?' selected':'') + '>Mathematical \u2014 Formula Application</option>'
      +       '<option value="diagrammatic_aoa"'       + (initType==='diagrammatic_aoa'       ?' selected':'') + '>Diagrammatic \u2014 Network Diagram (AOA)</option>'
      +       '<option value="multiple_choice"'        + (initType==='multiple_choice'        ?' selected':'') + '>Multiple Choice</option>'
      +       '<option value="true_false"'             + (initType==='true_false'             ?' selected':'') + '>True / False</option>'
      +     '</select>'
      +   '</div>'
      + '</div>'
      // ── 3. Answer area (model answer OR sub-question blocks) ───────────────
      + answerAreaHTML
      // ── 4. Scoring method ──────────────────────────────────────────────────
      + '<div class="ma-grading-block">'
      +   '<div class="ma-grading-row-hd">'
      +     '<span class="ma-grading-lbl">Scoring method</span>'
      +   '</div>'
      +   '<div class="qg-scoring-radios" data-gp="scoring"></div>'
      + '</div>'
      // ── 5. Additional checks ───────────────────────────────────────────────
      + '<div class="ma-grading-block">'
      +   '<span class="ma-grading-lbl">Additional checks</span>'
      +   '<div class="qg-checks" data-gp="checks"></div>'
      + '</div>'
      // ── 6. Custom instructions ─────────────────────────────────────────────
      + '<div class="ma-grading-block">'
      +   '<span class="ma-grading-lbl">Custom instructions</span>'
      +   '<textarea class="qg-textarea" data-gp="custom" rows="2"'
      +   ' placeholder=\'E.g. &quot;Accept notification as a valid example of push communication&quot;\'>'
      +   esc(initCustom) + '</textarea>'
      + '</div>';

    // ── _relabelSubBlocks: reassign a/b/c… after any deletion ────────────────
    function _relabelSubBlocks(wrap) {
      wrap.querySelectorAll('.ma-sub-block').forEach((blk, i) => {
        const lbl = String.fromCharCode(97 + i);
        blk.dataset.subIdx   = i;
        blk.dataset.subLabel = lbl;
        const lblEl = blk.querySelector('.ma-sub-label');
        if (lblEl) lblEl.textContent = '(' + lbl + ')';
        const ta = blk.querySelector('[data-field="sub-answer"]');
        if (ta) { ta.dataset.subIdx = i; ta.placeholder = 'Answer key for (' + lbl + ')\u2026'; }
      });
    }

    // ── _updateSubScoreWarn: show/hide the "sub marks exceed total" warning ─────
    function _updateSubScoreWarn() {
      const warn      = card.querySelector('.ma-sub-score-warn');
      if (!warn) return;
      const parentMax = parseFloat(card.querySelector('[data-field="score"]')?.value) || 0;
      const overrides = [...card.querySelectorAll('[data-field="sub-score-override"]')]
        .map(i => parseFloat(i.value) || 0);
      const anyOverride = overrides.some(v => v > 0);
      if (!anyOverride) { warn.classList.remove('show'); return; }
      const sum = overrides.reduce((a, b) => a + b, 0);
      warn.classList.toggle('show', parentMax > 0 && sum > parentMax + 0.0001);
    }

    // ── _buildSubBlock: create one fully-wired sub-question block ─────────────
    // subQImg / subAnsImg are optional base64 strings for pre-populated images
    // scoreOverride is an optional number (null/undefined = auto)
    function _buildSubBlock(idx, label, qText, answerKey, subQImg, subAnsImg, parentType, scoreOverride) {
      const block = document.createElement('div');
      block.className        = 'ma-sub-block';
      block.dataset.subIdx   = idx;
      block.dataset.subLabel = label;

      const _symHtml = _buildSymToolbarHTML();
      const _imgBtnSvg =
        '<svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2"/>'
        + '<circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>';

      // Mode always inherits from the parent question type.
      // Extract stored MC/TF data from answerKey for pre-population when parent type matches.
      const subAnsMode = parentType || 'short_answer';
      let subMcData = null, subTfData = null;
      if (answerKey) {
        try {
          const _ak = JSON.parse(answerKey);
          if (_ak.type === 'multiple_choice') subMcData = _ak;
          else if (_ak.type === 'true_false') subTfData = _ak;
        } catch (_) {}
      }
      // Use the answer text in the SA textarea for every mode except MC/TF,
      // which have their own dedicated editors (MC options, TF toggle).
      // The old check `=== 'short_answer'` was wrong: subAnsMode is always a
      // UI type code ('recall_general', 'mathematical', etc.) and never the
      // literal string 'short_answer', so the textarea was always left empty.
      const plainAnsKey = (subAnsMode === 'multiple_choice' || subAnsMode === 'true_false')
        ? ''
        : (answerKey || '');

      const _soVal = (scoreOverride != null && scoreOverride > 0) ? scoreOverride : '';

      block.innerHTML =
        // ── Header: label | optional marks | delete ───────────────────────────
        '<div class="ma-sub-hd">'
        + '<span class="ma-sub-label">(' + label + ')</span>'
        + '<div class="ma-sub-score-wrap">'
        +   '<span class="ma-sub-score-lbl">Marks</span>'
        +   '<input type="number" class="ma-sub-score-input" data-field="sub-score-override"'
        +   ' placeholder="Auto" step="0.25" min="0.01" autocomplete="off"'
        +   ' value="' + _soVal + '">'
        + '</div>'
        + '<button type="button" class="ma-sub-del" title="Remove sub-question">&times;</button>'
        + '</div>'
        // ── Sub-question text field ───────────────────────────────────────────
        + '<input type="text" class="ma-input field-input" data-field="sub-question"'
        + ' placeholder="Sub-question text\u2026" autocomplete="off"'
        + ' value="' + esc(qText || '') + '">'
        // Sub-question text toolbar
        + '<div class="ma-img-row">'
        +   '<button type="button" class="ma-img-btn" data-img-btn="sub-q" title="Attach image">'
        +     _imgBtnSvg + 'Add image'
        +   '</button>'
        +   '<input type="file" accept="image/*" style="display:none" data-img-input="sub-q">'
        +   '<input type="hidden" data-field="sub-q-img">'
        +   '<button type="button" class="ma-sym-toggle" data-sym-toggle="sub-q">\u03a9 Symbols</button>'
        + '</div>'
        + '<div data-img-preview="sub-q"></div>'
        + '<div class="ma-sym-toolbar" data-sym-toolbar="sub-q">' + _symHtml + '</div>'
        + '<div class="ma-field-err" data-err="sub-question">Sub-question text is required.</div>'
        // ── Answer wrap: SA textarea + MC/TF editors appended below ──────────
        + '<div class="ma-sub-ans-wrap">'
        +   '<div class="ma-sa-wrap">'
        +     '<textarea class="ma-textarea" data-field="sub-answer" data-sub-idx="' + idx + '"'
        +     ' placeholder="Answer key for (' + label + ')\u2026" rows="3">'
        +     esc(plainAnsKey)
        +     '</textarea>'
        +     '<div class="ma-img-row">'
        +       '<button type="button" class="ma-img-btn" data-img-btn="sub-ans" title="Attach image">'
        +         _imgBtnSvg + 'Add image'
        +       '</button>'
        +       '<input type="file" accept="image/*" style="display:none" data-img-input="sub-ans">'
        +       '<input type="hidden" data-field="sub-ans-img">'
        +       '<button type="button" class="ma-sym-toggle" data-sym-toggle="sub-ans">\u03a9 Symbols</button>'
        +     '</div>'
        +     '<div data-img-preview="sub-ans"></div>'
        +     '<div class="ma-sym-toolbar" data-sym-toolbar="sub-ans">' + _symHtml + '</div>'
        +     '<div class="ma-field-err" data-err="sub-answer">Answer key is required.</div>'
        +   '</div>'
        + '</div>';  // .ma-sub-ans-wrap

      // ── Append MC/TF editors to .ma-sub-ans-wrap ─────────────────────────
      const subAnsWrapEl = block.querySelector('.ma-sub-ans-wrap');
      subAnsWrapEl.appendChild(_buildMcEditor(subMcData));
      subAnsWrapEl.appendChild(_buildTfEditor(subTfData));
      _switchAnswerMode(subAnsWrapEl, subAnsMode);

      // ── Sub-block MC allow-sel → update card-level scoring section ────────
      // Uses "most permissive wins": if any sub-block has multiple correct,
      // show the full partial-credit scoring options, otherwise exact-match only.
      const subMcAllowSel = subAnsWrapEl.querySelector('.ma-mc-allow-sel');
      if (subMcAllowSel) {
        subMcAllowSel.addEventListener('change', () => {
          if (typeSelect.value === 'multiple_choice') {
            const anyMultiple = [...card.querySelectorAll('.ma-sub-block .ma-mc-allow-sel')]
              .some(s => s.value === 'multiple');
            const eff = anyMultiple ? 'multiple_choice' : 'multiple_choice_single';
            _renderScoringAndChecks(eff, DEFAULT_SCORING[eff], []);
          }
        });
      }

      // ── Dirty-state listeners ─────────────────────────────────────────────
      block.querySelectorAll('input[type="text"], input[type="number"], textarea').forEach(el => {
        el.addEventListener('input', () => { maIsSaved = false; hideMaSavedBadge(); updateNextBtn(); });
      });

      // ── Sub-score override: validate and update warning ───────────────────
      const scoreOverrideInput = block.querySelector('[data-field="sub-score-override"]');
      if (scoreOverrideInput) {
        scoreOverrideInput.addEventListener('input', () => {
          const raw = parseFloat(scoreOverrideInput.value);
          if (scoreOverrideInput.value !== '' && (isNaN(raw) || raw <= 0)) {
            scoreOverrideInput.style.borderColor = '#ef4444';
          } else {
            scoreOverrideInput.style.borderColor = '';
          }
          _updateSubScoreWarn();
        });
      }

      // ── Image upload wiring (scoped to block) ─────────────────────────────
      _wireImageUpload(block, 'sub-q');
      _wireImageUpload(block, 'sub-ans');

      // ── Symbol toolbar wiring ─────────────────────────────────────────────
      _wireSymbols(
        block.querySelector('[data-sym-toggle="sub-q"]'),
        block.querySelector('[data-sym-toolbar="sub-q"]'),
        block.querySelector('[data-field="sub-question"]')
      );
      _wireSymbols(
        block.querySelector('[data-sym-toggle="sub-ans"]'),
        block.querySelector('[data-sym-toolbar="sub-ans"]'),
        block.querySelector('[data-field="sub-answer"]')
      );

      // ── Pre-populate images if provided ───────────────────────────────────
      if (subQImg) {
        const h = block.querySelector('[data-field="sub-q-img"]');
        const p = block.querySelector('[data-img-preview="sub-q"]');
        if (h) { h.value = subQImg; if (p) _renderImgPreview(p, h, block.querySelector('[data-img-input="sub-q"]')); }
      }
      if (subAnsImg) {
        const h = block.querySelector('[data-field="sub-ans-img"]');
        const p = block.querySelector('[data-img-preview="sub-ans"]');
        if (h) { h.value = subAnsImg; if (p) _renderImgPreview(p, h, block.querySelector('[data-img-input="sub-ans"]')); }
      }

      // ── Delete button ─────────────────────────────────────────────────────
      block.querySelector('.ma-sub-del').addEventListener('click', () => {
        const wrap = card.querySelector('.ma-sub-questions-wrap');
        block.remove();
        _relabelSubBlocks(wrap);
        if (!wrap.querySelectorAll('.ma-sub-block').length) {
          card.removeAttribute('data-grouped');
        }
        maIsSaved = false; hideMaSavedBadge(); updateNextBtn();
      });

      return block;
    }

    // ── _addSubQuestion: activate grouped mode and append a new sub-block ─────
    function _addSubQuestion() {
      const wrap  = card.querySelector('.ma-sub-questions-wrap');
      const idx   = wrap.querySelectorAll('.ma-sub-block').length;
      const label = String.fromCharCode(97 + idx); // a, b, c …
      if (idx === 0) card.dataset.grouped = 'true';
      const block = _buildSubBlock(idx, label, '', '', '', '', typeSelect.value);
      wrap.appendChild(block);
      block.querySelector('[data-field="sub-question"]').focus();
      maIsSaved = false; hideMaSavedBadge(); updateNextBtn();
    }

    // ── Pre-populate sub-blocks when loading existing grouped data ────────────
    if (subQs) {
      card.dataset.grouped = 'true';
      const wrap = card.querySelector('.ma-sub-questions-wrap');
      subQs.forEach((sq, i) => {
        const label = sq.label || String.fromCharCode(97 + i);
        wrap.appendChild(_buildSubBlock(
          i, label,
          sq.question_text  || '', sq.answer_key || '',
          sq.sub_q_img      || '', sq.sub_ans_img || '',
          initType,
          sq.score_override != null ? parseFloat(sq.score_override) : null
        ));
      });
      _updateSubScoreWarn();
    }

    // ── Wire "+ Add sub-question" button ──────────────────────────────────────
    card.querySelector('.btn-add-sub-q').addEventListener('click', _addSubQuestion);

    // ── Build + append MC/TF editors to .ma-answer-single ────────────────────
    const answerSingleEl = card.querySelector('.ma-answer-single');
    let initMcData = null, initTfData = null;
    if (data.modelAnswer) {
      try {
        const _p = JSON.parse(data.modelAnswer);
        if (_p.type === 'multiple_choice') initMcData = _p;
        else if (_p.type === 'true_false') initTfData = _p;
      } catch (_) {}
    }
    answerSingleEl.appendChild(_buildMcEditor(initMcData));
    answerSingleEl.appendChild(_buildTfEditor(initTfData));
    _switchAnswerMode(answerSingleEl, initType);
    const mcAllowSel = answerSingleEl.querySelector('.ma-mc-allow-sel');

    // ── Field listeners ─────────────────────────────────────────────────────
    card.querySelectorAll('.ma-input, .ma-textarea, .ma-score-inline-input').forEach(el => {
      el.addEventListener('input', () => { maIsSaved = false; hideMaSavedBadge(); updateNextBtn(); });
    });

    // ── Image upload wiring ──────────────────────────────────────────────────
    _wireImageUpload(card, 'question');
    _wireImageUpload(card, 'answer');

    // ── Symbol toolbar wiring ────────────────────────────────────────────────
    const qInput    = card.querySelector('[data-field="question"]');
    const aTextarea = card.querySelector('[data-field="answer"]');
    _wireSymbols(
      card.querySelector('[data-sym-toggle="question"]'),
      card.querySelector('[data-sym-toolbar="question"]'),
      qInput
    );
    _wireSymbols(
      card.querySelector('[data-sym-toggle="answer"]'),
      card.querySelector('[data-sym-toolbar="answer"]'),
      aTextarea
    );

    const scoreEl = card.querySelector('.ma-score-inline-input');
    if (scoreEl) {
      scoreEl.addEventListener('input', function () {
        const raw     = this.value;
        const dotIdx  = raw.indexOf('.');
        const intPart = dotIdx === -1 ? raw : raw.slice(0, dotIdx);
        if (intPart.length > 3) {
          this.value = dotIdx === -1 ? intPart.slice(0, 3) : intPart.slice(0, 3) + raw.slice(dotIdx);
        }
        // Re-check sub-score warn whenever parent total changes
        _updateSubScoreWarn();
      });
    }

    // ── Grading control wiring ───────────────────────────────────────────────
    const typeSelect   = card.querySelector('[data-gp="type"]');
    const scoringGroup = card.querySelector('[data-gp="scoring"]');
    const checksGroup  = card.querySelector('[data-gp="checks"]');
    const customTA     = card.querySelector('[data-gp="custom"]');
    const radioName    ='scoring-' + num + '-' + Math.random().toString(36).slice(2, 6);

    function _defaultChecksFor(qType) {
      return (CHECKS_BY_TYPE[qType] || []).filter(c => c.def).map(c => c.val);
    }

    function _renderScoringAndChecks(qType, scoringVal, checksVals) {
      const _fallbackScoring = ['keyword_matching', 'semantic_similarity', 'holistic_judgment'];
      const scoringOpts  = SCORING_BY_TYPE[qType] || _fallbackScoring;
      const defScoring   = DEFAULT_SCORING[qType] || scoringOpts[0];
      scoringGroup.innerHTML = scoringOpts.map(v =>
        '<label class="qg-radio-label"><input type="radio" name="' + radioName + '" value="' + v + '"'
        + (v === scoringVal ? ' checked' : '') + '>'
        + SCORING_LABELS[v]
        + (v === defScoring ? '<span class="qg-radio-default"> — default</span>' : '')
        + '</label>'
      ).join('');

      const checkOpts = CHECKS_BY_TYPE[qType] || [];
      checksGroup.innerHTML = checkOpts.map(c =>
        '<label class="qg-check"><input type="checkbox" value="' + c.val + '"'
        + (checksVals.includes(c.val) ? ' checked' : '') + '> ' + c.label + '</label>'
      ).join('');

      // Hide the entire "Additional checks" block for types with no checks (MC, TF)
      const checksBlock = checksGroup.closest('.ma-grading-block');
      if (checksBlock) checksBlock.style.display = checkOpts.length ? '' : 'none';

    }

    // Initial render with saved/default state
    // For MC: effective type depends on single vs multiple mode.
    // For grouped MC cards: check sub-blocks (parent MC editor is hidden); for non-grouped: check parent editor.
    let _initEffType;
    if (initType === 'multiple_choice') {
      const _subAllowSels = [...card.querySelectorAll('.ma-sub-block .ma-mc-allow-sel')];
      const _anySubMultiple = _subAllowSels.some(s => s.value === 'multiple');
      const _parentMultiple = !!(initMcData && initMcData.allow_multiple);
      const _isMultiple = _subAllowSels.length ? _anySubMultiple : _parentMultiple;
      _initEffType = _isMultiple ? 'multiple_choice' : 'multiple_choice_single';
    } else {
      _initEffType = initType;
    }
    // When no scoringMethod was explicitly saved, derive the scoring value from
    // the *effective* type, not the base type.  This matters for MC questions:
    //   base type  'multiple_choice'        → DEFAULT_SCORING = 'partial_credit'
    //   eff  type  'multiple_choice_single' → DEFAULT_SCORING = 'exact_match'
    // Without this correction _renderScoringAndChecks receives scoringVal='partial_credit'
    // for a single-answer MC card — the value isn't in the option set, so no radio is
    // selected even though the option is labelled "— default".
    const _scoringForRender = data.scoringMethod
      ? initScoring
      : (DEFAULT_SCORING[_initEffType] || initScoring);
    _renderScoringAndChecks(_initEffType, _scoringForRender, initChecks);

    // Helper: resolve the "effective" type for MC (single vs multiple scoring options)
    function _effType(qType) {
      if (qType !== 'multiple_choice') return qType;
      return (mcAllowSel?.value === 'multiple') ? 'multiple_choice' : 'multiple_choice_single';
    }

    // Type change → reset defaults, re-render grading, switch answer mode
    typeSelect.addEventListener('change', function () {
      const newType    = this.value;
      const eff        = _effType(newType);
      const defScoring = DEFAULT_SCORING[eff] || 'keyword_matching';
      const defChecks  = _defaultChecksFor(eff);
      _renderScoringAndChecks(eff, defScoring, defChecks);
      _switchAnswerMode(answerSingleEl, newType);
      // Propagate new type to all sub-blocks (grouped questions inherit parent type)
      card.querySelectorAll('.ma-sub-block').forEach(blk => {
        const subWrap = blk.querySelector('.ma-sub-ans-wrap');
        if (subWrap) _switchAnswerMode(subWrap, newType);
      });
    });

    // MC allow-select change → update scoring options immediately
    if (mcAllowSel) {
      mcAllowSel.addEventListener('change', () => {
        if (typeSelect.value === 'multiple_choice') {
          const eff        = _effType('multiple_choice');
          const defScoring = DEFAULT_SCORING[eff];
          _renderScoringAndChecks(eff, defScoring, []);
        }
      });
    }

    return card;
  }

  function renumberQuestions() {
    document.querySelectorAll('#maQuestions .ma-question-card').forEach((card, i) => {
      const n = i + 1;
      card.dataset.num = n;
      const lbl = card.querySelector('.ma-q-label');
      if (lbl) lbl.textContent = 'Question ' + n;
    });
    updateMaCount();
  }

  /* ── Drag-and-drop reorder ─────────────────────────────────────────────── */
  (function _initDragReorder() {
    const container = document.getElementById('maQuestions');
    if (!container) return;

    // Only enable drag when pressing the handle; disable it everywhere else
    container.addEventListener('mousedown', function (e) {
      const handle = e.target.closest('.ma-drag-handle');
      const card   = e.target.closest('.ma-question-card');
      if (!card) return;
      card.draggable = !!handle;
    }, true);

    container.addEventListener('dragstart', function (e) {
      const card = e.target.closest('.ma-question-card');
      if (!card) return;
      _dragCard = card;
      // Slight delay so the drag image captures the un-dimmed card
      requestAnimationFrame(() => card.classList.add('dragging'));
      e.dataTransfer.effectAllowed = 'move';
    });

    container.addEventListener('dragend', function () {
      if (_dragCard) {
        _dragCard.classList.remove('dragging');
        _dragCard.draggable = false;
      }
      _dragCard = null;
      container.querySelectorAll('.ma-question-card').forEach(c => {
        c.classList.remove('drag-over-top', 'drag-over-bottom');
      });
    });

    container.addEventListener('dragover', function (e) {
      e.preventDefault();
      const target = e.target.closest('.ma-question-card');
      if (!target || target === _dragCard) return;

      // Clear indicators on all other cards
      container.querySelectorAll('.ma-question-card').forEach(c => {
        if (c !== target) c.classList.remove('drag-over-top', 'drag-over-bottom');
      });

      const rect = target.getBoundingClientRect();
      const mid  = rect.top + rect.height / 2;
      if (e.clientY < mid) {
        target.classList.add('drag-over-top');
        target.classList.remove('drag-over-bottom');
      } else {
        target.classList.add('drag-over-bottom');
        target.classList.remove('drag-over-top');
      }
      e.dataTransfer.dropEffect = 'move';
    });

    container.addEventListener('dragleave', function (e) {
      const target = e.target.closest('.ma-question-card');
      if (target) {
        target.classList.remove('drag-over-top', 'drag-over-bottom');
      }
    });

    container.addEventListener('drop', function (e) {
      e.preventDefault();
      const target = e.target.closest('.ma-question-card');
      if (!target || !_dragCard || target === _dragCard) return;

      const isTop = target.classList.contains('drag-over-top');
      target.classList.remove('drag-over-top', 'drag-over-bottom');

      if (isTop) {
        container.insertBefore(_dragCard, target);
      } else {
        container.insertBefore(_dragCard, target.nextSibling);
      }

      renumberQuestions();
      maIsSaved = false;
      hideMaSavedBadge();
      updateNextBtn();
    });
  })();

  /* ── Clear Answer Key modal ─────────────────────────────────────────────── */
  (function _initClearAnswerKey() {
    const btnClear   = document.getElementById('btnClearAnswerKey');
    const modal      = document.getElementById('maDeleteAllModal');
    const msgEl      = document.getElementById('maDeleteAllMsg');
    const cancelBtn  = document.getElementById('maDeleteAllCancelBtn');
    const confirmBtn = document.getElementById('maDeleteAllConfirmBtn');
    if (!btnClear || !modal) return;

    function _closeModal() { modal.classList.remove('open'); }

    btnClear.addEventListener('click', function () {
      const cards = document.querySelectorAll('#maQuestions .ma-question-card');
      if (!cards.length) return;
      const n = cards.length;
      msgEl.innerHTML = 'This will permanently delete all <strong>' + n + '</strong> question' + (n === 1 ? '' : 's') + '.<br>This action cannot be undone.';
      modal.classList.add('open');
    });

    cancelBtn.addEventListener('click', _closeModal);
    modal.addEventListener('click', function (e) {
      if (e.target === modal) _closeModal();
    });

    confirmBtn.addEventListener('click', async function () {
      // Do not render empty state while answer key extraction is active.
      // This prevents UI regression where progress strip disappears during background extraction.
      if (_akExtractionActive) { _closeModal(); return; }
      _closeModal();
      const container = document.getElementById('maQuestions');
      const cards = [...container.querySelectorAll('.ma-question-card')];
      if (!cards.length) return;

      confirmBtn.disabled = true;
      cancelBtn.disabled  = true;

      // Collect all server IDs to delete
      const idsToDelete = cards
        .map(c => c.dataset.answerId ? parseInt(c.dataset.answerId, 10) : null)
        .filter(Boolean);

      if (idsToDelete.length) {
        const results = await Promise.allSettled(
          idsToDelete.map(id => apiFetch('/model-answers/' + id, { method: 'DELETE' }))
        );

        const failedIds = new Set();
        results.forEach((r, i) => {
          if (r.status === 'fulfilled') {
            serverAnswerIds.delete(idsToDelete[i]);
          } else {
            failedIds.add(idsToDelete[i]);
          }
        });

        if (failedIds.size > 0) {
          // Remove only the successfully deleted cards; leave failed ones in place
          cards.forEach(card => {
            const id = card.dataset.answerId ? parseInt(card.dataset.answerId, 10) : null;
            if (id === null || !failedIds.has(id)) card.remove();
          });
          renumberQuestions();
          maIsSaved = false;
          hideMaSavedBadge();
          updateMaCount();
          updateNextBtn();
          const remaining = container.querySelectorAll('.ma-question-card').length;
          if (!remaining) {
            container.innerHTML = '<p class="ma-empty">No questions added yet. Click \u201cAdd Question\u201d to begin.</p>';
          }
          toast(failedIds.size + ' question' + (failedIds.size === 1 ? '' : 's') + ' could not be deleted \u2014 please try again.', 'error');
          confirmBtn.disabled = false;
          cancelBtn.disabled  = false;
          return;
        }
      }

      container.innerHTML = '<p class="ma-empty">No questions added yet. Click \u201cAdd Question\u201d to begin.</p>';
      maIsSaved = false;
      hideMaSavedBadge();
      updateMaCount();
      updateNextBtn();
      toast('Answer key cleared.', 'success');

      confirmBtn.disabled = false;
      cancelBtn.disabled  = false;
    });
  })();

  function updateMaCount() {
    const count = document.querySelectorAll('#maQuestions .ma-question-card').length;
    const badge = document.getElementById('maCount');
    if (badge) {
      badge.textContent   = count + (count === 1 ? ' question' : ' questions');
      badge.style.display = count ? 'inline-flex' : 'none';
    }
    updateExamSummary();
  }

  function updateExamSummary() {
    const panel = document.getElementById('maExamSummary');
    if (!panel) return;

    const cards = [...document.querySelectorAll('#maQuestions .ma-question-card')];
    const hasExam  = !!maCurrentExamId;
    const hasCards = cards.length > 0;

    if (!hasExam || !hasCards) {
      panel.style.display = 'none';
      return;
    }
    panel.style.display = '';

    // --- Question count ---
    document.getElementById('maSummaryQCount').textContent = cards.length;

    // --- Answer key total (used for mismatch check only) ---
    let akTotal = 0;
    cards.forEach(card => {
      const val = parseFloat(card.querySelector('.ma-score-inline-input')?.value) || 0;
      akTotal += val;
    });

    // --- Exam official total ---
    const exam = (typeof exams !== 'undefined' ? exams : []).find(e => e.id === maCurrentExamId);
    const examTotal = exam?.totalMarks ?? null;
    const examTotalEl = document.getElementById('maSummaryExamTotal');
    examTotalEl.textContent = examTotal !== null ? examTotal : '—';

    // --- Mismatch warning ---
    const mismatch = document.getElementById('maSummaryMismatch');
    if (examTotal !== null && akTotal !== examTotal) {
      mismatch.style.display = '';
    } else {
      mismatch.style.display = 'none';
    }

    // --- Nav grid ---
    const dropdown = document.getElementById('maSummaryNavDropdown');
    dropdown.innerHTML = '';

    // Title
    const title = document.createElement('div');
    title.className = 'ma-nav-panel-title';
    title.textContent = 'Question Navigator';
    dropdown.appendChild(title);

    // Grid of number boxes
    const grid = document.createElement('div');
    grid.className = 'ma-nav-grid';
    cards.forEach((card, i) => {
      const qNum  = i + 1;
      const status = card.dataset.navStatus || 'pristine';
      const box = document.createElement('button');
      box.type = 'button';
      box.className = 'ma-nav-box';
      box.dataset.status = status;
      box.dataset.navIndex = i;
      box.textContent = qNum;
      box.title = 'Jump to Question ' + qNum;
      box.addEventListener('click', () => {
        card.scrollIntoView({ behavior: 'smooth', block: 'start' });
        _closeSummaryNav();
      });
      grid.appendChild(box);
    });
    dropdown.appendChild(grid);

    // Legend
    const legend = document.createElement('div');
    legend.className = 'ma-nav-legend';
    legend.innerHTML =
      '<span class="ma-nav-legend-item"><span class="ma-nav-legend-dot ma-nav-legend-dot--pristine"></span>Not edited</span>' +
      '<span class="ma-nav-legend-item"><span class="ma-nav-legend-dot ma-nav-legend-dot--edited"></span>Edited</span>' +
      '<span class="ma-nav-legend-item"><span class="ma-nav-legend-dot ma-nav-legend-dot--saved"></span>Saved</span>';
    dropdown.appendChild(legend);

    // Mark active card (most visible in scroll container)
    _updateNavActiveBox();
  }

  function _closeSummaryNav() {
    const btn      = document.getElementById('maSummaryNavBtn');
    const dropdown = document.getElementById('maSummaryNavDropdown');
    if (btn)      btn.setAttribute('aria-expanded', 'false');
    if (dropdown) dropdown.classList.remove('open');
  }

  function _updateNavActiveBox() {
    const cards   = [...document.querySelectorAll('#maQuestions .ma-question-card')];
    const boxes   = [...document.querySelectorAll('#maSummaryNavDropdown .ma-nav-box')];
    if (!cards.length || !boxes.length) return;

    // Find which card's top is closest to (but not past) the scroll container top
    const scrollEl = document.querySelector('main') || document.documentElement;
    const scrollTop = scrollEl.scrollTop;
    let activeIdx = 0;
    cards.forEach((card, i) => {
      const cardTop = card.offsetTop - (scrollEl.offsetTop || 0);
      if (cardTop <= scrollTop + 80) activeIdx = i; // 80px grace so title row counts
    });

    boxes.forEach((box, i) => box.classList.toggle('active', i === activeIdx));
  }

  function _positionNavDropdown() {
    const trigger  = document.getElementById('maSummaryNavBtn');
    const dropdown = document.getElementById('maSummaryNavDropdown');
    if (!trigger || !dropdown) return;

    const GAP      = 8;   // px gap between trigger bottom and panel top
    const MARGIN   = 10;  // minimum distance from viewport edges

    const tRect = trigger.getBoundingClientRect();
    const vw    = window.innerWidth;

    // Start centered under the trigger
    const panelW = Math.min(340, vw - MARGIN * 2);
    dropdown.style.width = panelW + 'px';

    let left = tRect.left + tRect.width / 2 - panelW / 2;

    // Clamp: don't overflow right edge
    if (left + panelW + MARGIN > vw) left = vw - panelW - MARGIN;
    // Clamp: don't overflow left edge
    if (left < MARGIN) left = MARGIN;

    dropdown.style.top  = (tRect.bottom + GAP) + 'px';
    dropdown.style.left = left + 'px';
  }

  function addModelQuestion() {
    const container = document.getElementById('maQuestions');
    const empty     = container.querySelector('.ma-empty');
    if (empty) empty.remove();
    const count = container.querySelectorAll('.ma-question-card').length;
    const card  = buildQuestionCard(count + 1, {});
    container.appendChild(card);
    card.querySelector('[data-field="question"]').focus();
    updateMaCount();
    maIsSaved = false;
    hideMaSavedBadge();
    updateNextBtn();
  }

  // Remove a question card — confirms with user, then deletes from backend if persisted.
  window.removeModelQuestion = function (btn) {
    _pendingDeleteBtn = btn;
    _qDelModal.classList.add('open');
  };

  // ── Question-delete modal wiring ────────────────────────────────────────────
  let _pendingDeleteBtn = null;
  const _qDelModal      = document.getElementById('qDelModal');

  function _closeQDelModal() {
    _qDelModal.classList.remove('open');
    _pendingDeleteBtn = null;
  }

  document.getElementById('qDelCancelBtn').addEventListener('click', _closeQDelModal);
  _qDelModal.addEventListener('click', e => { if (e.target === e.currentTarget) _closeQDelModal(); });

  document.getElementById('qDelConfirmBtn').addEventListener('click', async function () {
    const btn = _pendingDeleteBtn;
    if (!btn) return;
    _closeQDelModal();
    await _doDeleteQuestion(btn);
  });

  async function _doDeleteQuestion(btn) {
    // Do not render empty state while answer key extraction is active.
    // This prevents UI regression where progress strip disappears during background extraction.
    if (_akExtractionActive) return;
    const card = btn.closest('.ma-question-card');
    if (!card) return;

    const answerId = card.dataset.answerId
      ? parseInt(card.dataset.answerId, 10)
      : null;

    if (answerId) {
      btn.disabled = true;
      try {
        await apiFetch('/model-answers/' + answerId, { method: 'DELETE' });
        serverAnswerIds.delete(answerId);
      } catch (err) {
        toast('Failed to delete question \u2014 ' + (err.message || 'unknown error'), 'error');
        btn.disabled = false;
        return;
      }
    }

    card.remove();
    renumberQuestions();

    if (!document.querySelectorAll('#maQuestions .ma-question-card').length) {
      document.getElementById('maQuestions').innerHTML =
        '<p class="ma-empty">No questions added yet. Click \u201cAdd Question\u201d to begin.</p>';
      updateMaCount();
    }

    maIsSaved = false;
    hideMaSavedBadge();
    updateNextBtn();
  };

  function validateCard(card) {
    const isGrouped = card.dataset.grouped === 'true';
    let valid = true;

    // Fields shared by both grouped and non-grouped cards
    const checks = [
      { sel: '[data-field="question"]', ok: (v) => v.trim() !== '' || !!card.querySelector('[data-field="question-img"]')?.value },
      { sel: '[data-field="score"]',    ok: (v) => { const s = v.trim(); return s !== '' && /^\d{1,3}(\.\d+)?$/.test(s) && parseFloat(s) > 0; } },
    ];

    checks.forEach(c => {
      const el   = card.querySelector(c.sel);
      if (!el) return;
      const err    = el.parentElement.querySelector('.ma-field-err');
      const passes = c.ok(el.value, el);
      el.classList.toggle('invalid', !passes);
      if (err) err.classList.toggle('show', !passes);
      if (!passes) valid = false;
    });

    // Validate answer area for non-grouped cards
    if (!isGrouped) {
      const answerSingleEl = card.querySelector('.ma-answer-single');
      const ansMode = answerSingleEl?.dataset.ansMode || 'short_answer';
      if (ansMode === 'multiple_choice' || ansMode === 'true_false') {
        if (!_validateAnswerContainer(answerSingleEl)) valid = false;
      } else {
        // short_answer: validate textarea or image
        const ansEl  = card.querySelector('[data-field="answer"]');
        const passes = !ansEl || ansEl.value.trim() !== '' || !!card.querySelector('[data-field="answer-img"]')?.value;
        if (ansEl) ansEl.classList.toggle('invalid', !passes);
        const err = ansEl?.parentElement?.querySelector('.ma-field-err');
        if (err) err.classList.toggle('show', !passes);
        if (!passes) valid = false;
      }
    }

    // Validate sub-question text + answer in grouped mode
    if (isGrouped) {
      card.querySelectorAll('.ma-sub-block').forEach(block => {
        // Sub-question text is always required
        const qtEl  = block.querySelector('[data-field="sub-question"]');
        const qtErr = block.querySelector('[data-err="sub-question"]');
        if (qtEl) {
          const passes = qtEl.value.trim() !== '';
          qtEl.classList.toggle('invalid', !passes);
          if (qtErr) qtErr.classList.toggle('show', !passes);
          if (!passes) valid = false;
        }

        // Answer key depends on sub-block answer mode
        const subAnsWrap = block.querySelector('.ma-sub-ans-wrap');
        const subAnsMode = subAnsWrap?.dataset.ansMode || 'short_answer';
        if (subAnsMode === 'multiple_choice' || subAnsMode === 'true_false') {
          if (!_validateAnswerContainer(subAnsWrap)) valid = false;
        } else {
          const ansEl  = block.querySelector('[data-field="sub-answer"]');
          const ansErr = block.querySelector('[data-err="sub-answer"]');
          if (ansEl) {
            const passes = ansEl.value.trim() !== '';
            ansEl.classList.toggle('invalid', !passes);
            if (ansErr) ansErr.classList.toggle('show', !passes);
            if (!passes) valid = false;
          }
        }

        // Score override: if filled, must be a positive number
        const soEl = block.querySelector('[data-field="sub-score-override"]');
        if (soEl && soEl.value.trim() !== '') {
          const soNum = parseFloat(soEl.value);
          const soOk  = !isNaN(soNum) && soNum > 0;
          soEl.style.borderColor = soOk ? '' : '#ef4444';
          if (!soOk) valid = false;
        }
      });
    }

    card.classList.toggle('card-invalid', !valid);
    return valid;
  }

  function showMaSavedBadge() {
    const saved    = document.getElementById('maSavedBadge');
    const unsaved  = document.getElementById('maUnsavedBadge');
    if (saved)   saved.classList.add('show');
    if (unsaved) unsaved.classList.remove('show');
  }
  function hideMaSavedBadge() {
    const saved   = document.getElementById('maSavedBadge');
    const unsaved = document.getElementById('maUnsavedBadge');
    if (saved) saved.classList.remove('show');
    // Show "Unsaved changes" only when an exam is active and there is at least one card
    if (unsaved) {
      const hasCards = !!document.querySelector('#maQuestions .ma-question-card');
      unsaved.classList.toggle('show', !!(maCurrentExamId && hasCards));
    }
  }

  // Warn the user before leaving the page if there are unsaved changes
  window.addEventListener('beforeunload', e => {
    if (maCurrentExamId && !maIsSaved && document.querySelector('#maQuestions .ma-question-card')) {
      e.preventDefault();
      e.returnValue = '';
    }
  });

  // Diagnostic: detect any real page navigation while extraction is active
  window.addEventListener('beforeunload', () => {
    if (_akExtractionActive) {
      console.error('[AK] PAGE UNLOAD DETECTED during extraction — check console for cause');
    }
  });

  /* ════════════════════════════════════════════════════════════════════════
     Load model answers from backend — GET /model-answers/?exam_id=
  ════════════════════════════════════════════════════════════════════════ */
  async function loadModelAnswersForExam(examId) {
    console.trace('[RUBRICS-TRACE] loadModelAnswersForExam examId=' + examId);
    // CRITICAL: Never render during active extraction.
    // The backend deletes old model-answer rows synchronously before returning
    // 202, so any GET /model-answers during that window returns [].
    // Rendering [] would blank the card container — "0 answers" here means
    // "extraction in flight", NOT "no answer key".
    // The extraction poll (and the global banner poll after reload recovery)
    // handle rendering via _renderExtractedAnswers / _rubricsUpdateModelAnswerSection
    // once rows.length > 0; this function must stay out of the way until then.
    const _akSsActive = typeof window._akExtractionKeyActive === 'function'
      && window._akExtractionKeyActive(examId);
    if (_akExtractionActive || _akSsActive) {
      console.warn('[RUBRICS-GUARD] loadModelAnswersForExam suppressed — extraction in flight (local=' + _akExtractionActive + ' session=' + _akSsActive + '). 0 rows ≠ empty state.');
      return;
    }

    const container = document.getElementById('maQuestions');

    // ── Fetch first, guard second, clear third ────────────────────────────────
    // CRITICAL: do NOT clear container.innerHTML before the await.  Clearing
    // before the fetch creates a TOCTOU window: if the user triggers an upload
    // between the guard above and the container clear, _akExtractionActive
    // becomes true, but the clear already ran — erasing the extraction overlay
    // and question cards that were just placed there.
    //
    // By fetching first and clearing only after the guard re-check, we ensure
    // the container is never wiped while extraction is in flight.
    let answers;
    try {
      answers = await apiFetch('/model-answers/?exam_id=' + examId);
    } catch (e) {
      // If extraction started while the fetch was in-flight, stay silent —
      // the extraction success path will handle the final render.
      if (_akExtractionActive) return;
      toast('Failed to load answer key.', 'error');
      container.innerHTML =
        '<p class="ma-empty">No questions added yet. Click \u201cAdd Question\u201d to begin.</p>';
      updateMaCount();
      updateNextBtn();
      return;
    }

    // Post-fetch guard: if extraction started while the request was in flight,
    // do NOT touch the container — the extraction overlay and strip are active.
    if (_akExtractionActive) return;

    // Safe to clear — extraction is not active
    container.innerHTML = '';
    hideMaSavedBadge();
    maIsSaved       = false;
    serverAnswerIds = new Set();

    console.log('[LOAD-1] loadModelAnswersForExam examId=', examId, '=> DB returned', answers.length, 'answers');
    answers.forEach(a => console.log(`  id=${a.id} q#${a.questionNumber} text="${(a.questionText||'').slice(0,50)}" score=${a.score}`));
    if (answers.length) {
      // answers are ordered by questionNumber from backend
      answers.forEach(a => {
        // Merge saved grading config (localStorage) if available for this question
        const gpCfg = (window._pendingGPRules || {})[a.questionNumber] || {};

        // Detect sub-questions serialized by saveCurrentModelAnswers
        let subQs = null;
        let plainAnswer = a.modelAnswer;
        if (a.modelAnswer && a.modelAnswer.startsWith('{"__sub__":')) {
          try {
            const parsed = JSON.parse(a.modelAnswer);
            if (Array.isArray(parsed.__sub__)) {
              subQs      = parsed.__sub__;
              plainAnswer = '';
            }
          } catch (_) {}
        }

        const card = buildQuestionCard(a.questionNumber, {
          answerId:          a.id,
          questionText:      a.questionText,
          modelAnswer:       plainAnswer,
          sub_questions:     subQs,
          score:             a.score,
          questionType:      a.questionType || gpCfg.question_type,
          scoringMethod:     gpCfg.scoring_method,
          checks:            gpCfg.checks,
          customInstruction: gpCfg.custom_instruction,
        });
        _restoreCardImages(card, examId, a.id);
        card.dataset.navStatus = 'saved';
        container.appendChild(card);
        serverAnswerIds.add(a.id);
      });
      window._pendingGPRules = null;
      maIsSaved = true;
      showMaSavedBadge();
    } else {
      container.innerHTML =
        '<p class="ma-empty">No questions added yet. Click \u201cAdd Question\u201d to begin.</p>';
    }
    updateMaCount();
    updateNextBtn();
  }

  /* ── Enable/gate the Answer Key + Grading Policy sections on exam change ─── */
  async function updateModelAnswerSection(examId) {
    // Guard against re-renders during active extraction (local flag OR
    // sessionStorage key set by the global banner on reload recovery).
    const _ssActive = examId && typeof window._akExtractionKeyActive === 'function'
      && window._akExtractionKeyActive(examId);
    if (_akExtractionActive || _ssActive) return;

    const section = document.getElementById('modelAnswerSection');
    if (!section) return;
    if (!examId) {
      section.classList.add('ak-gated');
      maCurrentExamId = null;
      maIsSaved       = false;
      serverAnswerIds = new Set();
      const summaryPanel = document.getElementById('maExamSummary');
      if (summaryPanel) summaryPanel.style.display = 'none';
      return;
    }
    maCurrentExamId = examId;
    section.classList.remove('ak-gated');
    loadGradingPolicy(examId);        // restore global rules + stage per-Q rules
    await loadModelAnswersForExam(examId);
  }

  // Expose globally so the banner's reload-recovery poll can trigger card
  // rendering after a page reload mid-extraction.
  window._rubricsUpdateModelAnswerSection = updateModelAnswerSection;

  // Expose force-unlock so the banner's cancel/404-auto-stop can release
  // _akExtractionActive without needing direct access to the closed-over var.
  window._akForceUnlock = function () {
    if (_akExtractionActive) {
      _akExtractionActive = false;
      console.log('[AK] _akForceUnlock — _akExtractionActive reset to false');
    }
  };

  /* ════════════════════════════════════════════════════════════════════════
     Save model answers — POST /model-answers/ + PUT /model-answers/{id}
                        + DELETE /model-answers/{id} for removed cards
  ════════════════════════════════════════════════════════════════════════ */
  async function saveCurrentModelAnswers() {
    if (!maCurrentExamId) return;

    const cards = [...document.querySelectorAll('#maQuestions .ma-question-card')];
    if (!cards.length) { toast('Add at least one question before saving.', 'info'); return; }

    console.log('[SAVE-1] saveCurrentModelAnswers: total cards in DOM =', cards.length);
    let allValid = true;
    const cardData = [];
    cards.forEach(card => {
      if (!validateCard(card)) {
        allValid = false;
        const qv = card.querySelector('[data-field="question"]')?.value?.trim() || '';
        const av = card.querySelector('[data-field="answer"]')?.value?.trim() || '';
        const qi = card.querySelector('[data-field="question-img"]')?.value || '';
        const ai = card.querySelector('[data-field="answer-img"]')?.value || '';
        const sv = card.querySelector('[data-field="score"]')?.value?.trim() || '';
        const sn = parseFloat(sv);
        console.warn(
          `[SAVE-1] INVALID card #${card.dataset.num}:`,
          `qOk=${!!qv || !!qi} (q="${qv.slice(0,30)}" qi=${!!qi})`,
          `aOk=${!!av || !!ai} (a="${av.slice(0,30)}" ai=${!!ai})`,
          `scoreOk=${sv !== '' && /^\d{1,3}(\.\d+)?$/.test(sv) && sn > 0} (sv="${sv}" sn=${sn})`
        );
        return;
      }
      const qText = card.querySelector('[data-field="question"]').value.trim()
        || (card.querySelector('[data-field="question-img"]')?.value ? '[See attached image]' : '');
      const isGrouped = card.dataset.grouped === 'true';
      let aText;
      if (isGrouped) {
        const subAnswers = [...card.querySelectorAll('.ma-sub-block')].map(block => {
          const subAnsWrap  = block.querySelector('.ma-sub-ans-wrap');
          const subAnsMode  = subAnsWrap?.dataset.ansMode || 'short_answer';
          let answerKey;
          if (subAnsMode === 'multiple_choice' || subAnsMode === 'true_false') {
            answerKey = _collectAnswerKey(subAnsWrap) || '';
          } else {
            answerKey = (block.querySelector('[data-field="sub-answer"]')?.value || '').trim();
          }
          const soRaw = block.querySelector('[data-field="sub-score-override"]')?.value?.trim();
          const soNum = soRaw !== '' && soRaw != null ? parseFloat(soRaw) : null;
          return {
            label:          block.dataset.subLabel || '',
            question_text:  (block.querySelector('[data-field="sub-question"]')?.value || '').trim(),
            answer_key:     answerKey,
            sub_q_img:      (block.querySelector('[data-field="sub-q-img"]')?.value    || ''),
            sub_ans_img:    (block.querySelector('[data-field="sub-ans-img"]')?.value  || ''),
            score_override: (soNum != null && soNum > 0) ? soNum : null,
          };
        });
        aText = JSON.stringify({ __sub__: subAnswers });
      } else {
        const answerSingleEl = card.querySelector('.ma-answer-single');
        const ansMode = answerSingleEl?.dataset.ansMode || 'short_answer';
        if (ansMode === 'multiple_choice' || ansMode === 'true_false') {
          aText = _collectAnswerKey(answerSingleEl) || '';
        } else {
          aText = card.querySelector('[data-field="answer"]').value.trim()
            || (card.querySelector('[data-field="answer-img"]')?.value ? '[See attached image]' : '');
        }
      }
      cardData.push({
        answerId:     card.dataset.answerId ? parseInt(card.dataset.answerId, 10) : null,
        num:          parseInt(card.dataset.num, 10),
        questionText: qText,
        modelAnswer:  aText,
        score:        parseFloat(card.querySelector('[data-field="score"]').value),
        questionType: card.querySelector('[data-gp="type"]')?.value || 'recall_general',
        cardEl:       card,
      });
    });

    console.log('[SAVE-1] allValid=', allValid, '| valid cardData.length=', cardData.length);
    if (!allValid) {
      toast('Please fill all required fields (question text, model answer, score) before saving.', 'error');
      return;
    }

    const btn = document.getElementById('btnSaveModelAnswers');
    btn.disabled = true;

    try {
      // IDs currently in DOM
      const currentIds = new Set(cardData.filter(d => d.answerId).map(d => d.answerId));

      // DELETE entries that were removed from the DOM since last load/save
      let _dbgDelCount = 0;
      for (const id of serverAnswerIds) {
        if (!currentIds.has(id)) {
          console.log('[SAVE-2] DELETE /model-answers/', id);
          await apiFetch('/model-answers/' + id, { method: 'DELETE' });
          _dbgDelCount++;
        }
      }
      console.log('[SAVE-2] DELETEs fired:', _dbgDelCount);

      // PUT existing cards / POST new cards
      const newServerIds = new Set();
      let _dbgSaveOk = 0;
      for (const d of cardData) {
        if (d.answerId) {
          // Existing entry — partial update
          console.log(`[SAVE-3] PUT /model-answers/${d.answerId} num=${d.num} score=${d.score}`);
          await apiFetch('/model-answers/' + d.answerId, {
            method: 'PUT',
            body:   JSON.stringify({
              questionNumber: d.num,
              questionText:   d.questionText,
              modelAnswer:    d.modelAnswer,
              score:          d.score,
              questionType:   d.questionType,
            }),
          });
          _persistCardImages(d.cardEl, maCurrentExamId, d.answerId);
          newServerIds.add(d.answerId);
          _dbgSaveOk++;
        } else {
          // New entry — create and stamp the returned id onto the card
          console.log(`[SAVE-3] POST /model-answers/ num=${d.num} score=${d.score} q="${d.questionText.slice(0,40)}" ans="${d.modelAnswer.slice(0,40)}"`);
          const created = await apiFetch('/model-answers/', {
            method: 'POST',
            body:   JSON.stringify({
              examId:         maCurrentExamId,
              questionNumber: d.num,
              questionText:   d.questionText,
              modelAnswer:    d.modelAnswer,
              score:          d.score,
              questionType:   d.questionType,
            }),
          });
          console.log(`[SAVE-3] POST succeeded, created.id=${created.id}`);
          d.cardEl.dataset.answerId = created.id;
          _persistCardImages(d.cardEl, maCurrentExamId, created.id);
          newServerIds.add(created.id);
          _dbgSaveOk++;
        }
      }
      console.log('[SAVE-3] Total saves succeeded:', _dbgSaveOk, '/', cardData.length);

      serverAnswerIds = newServerIds;
      // Mark all cards as saved and refresh the nav
      document.querySelectorAll('#maQuestions .ma-question-card').forEach(c => { c.dataset.navStatus = 'saved'; });
      maIsSaved = true;
      showMaSavedBadge();
      updateExamSummary();
      updateNextBtn();

      // Snapshot the current grading policy alongside the answer key save
      _snapshotGradingPolicy();
      toast('Answer key saved successfully.', 'success');

    } catch (err) {
      console.error('[SAVE-ERR] save loop threw:', err.message);
      toast('Save failed \u2014 ' + err.message, 'error');
    } finally {
      btn.disabled = false;
    }
  }

  /* ════════════════════════════════════════════════════════════════════════
     Answer Key Export  — CSV / Excel (.xlsx) / PDF
     Uses saved DB data (GET /model-answers/) + localStorage grading policy.
     No backend changes required — all processing is client-side.
  ════════════════════════════════════════════════════════════════════════ */

  // Lazily loads SheetJS (XLSX) from CDN only when user requests Excel export.
  async function _loadSheetJS() {
    if (window.XLSX) return;
    await new Promise((resolve, reject) => {
      const s    = document.createElement('script');
      s.src      = 'https://cdnjs.cloudflare.com/ajax/libs/xlsx/0.18.5/xlsx.full.min.js';
      s.onload   = resolve;
      s.onerror  = () => reject(new Error('Failed to load Excel library. Please check your internet connection.'));
      document.head.appendChild(s);
    });
  }

  // Build a reverse map: check value → human-readable label.
  function _buildCheckLabelMap() {
    const map = {};
    Object.values(CHECKS_BY_TYPE).forEach(arr => {
      arr.forEach(c => { map[c.val] = c.label; });
    });
    return map;
  }

  const _ANS_TYPE_LABEL = {
    recall_general:           'Open-ended (Recall)',
    recall_differentiation:   'Open-ended (Differentiation)',
    mathematical:             'Mathematical',
    diagrammatic_aoa:         'Diagrammatic (AOA)',
    multiple_choice:          'MCQ',
    multiple_choice_single:   'MCQ (Single select)',
    true_false:               'True / False',
    other:                    'Other',
  };

  const _SCORING_LABEL = {
    keyword_matching:    'Keyword Matching',
    semantic_similarity: 'Semantic Similarity',
    strict_match:        'Strict Match',
    rubric_based:        'Rubric Based',
  };

  const _EXPORT_COLS = [
    'QuestionID','SubQuestion','QuestionText','AnswerType',
    'CorrectAnswer','Marks','Options','ScoringMethod','Checks','CustomInstructions',
  ];

  /**
   * Parse one DB ExamModelAnswer record into one or more flat export row objects.
   * Handles: plain text, MCQ, True/False, grouped sub-questions (each sub can be
   * a different type), and mixed grouped-MCQ sub-questions.
   */
  function _parseModelAnswerForExport(ma, qPolicy, chkMap) {
    const qId     = 'Q' + ma.questionNumber;
    const qTxt    = ma.questionText || '';
    const marks   = ma.score != null ? ma.score : '';
    const scoring = _SCORING_LABEL[qPolicy.scoring_method] || qPolicy.scoring_method || '';
    const checks  = (qPolicy.checks || []).map(v => chkMap[v] || v).join('; ');
    const custom  = qPolicy.custom_instruction || '';
    const qType   = _ANS_TYPE_LABEL[qPolicy.question_type] || qPolicy.question_type || 'Open-ended';

    function _mcqRow(rowId, subLbl, subQTxt, mcqObj, rowMarks) {
      const ALPHA   = 'ABCDEFGHIJ';
      const opts    = mcqObj.options || [];
      const corrIdx = opts.findIndex(o => o.correct);
      const corrLtr = corrIdx >= 0 ? ALPHA[corrIdx] : '';
      const corrTxt = corrIdx >= 0 ? (opts[corrIdx].text || '') : '';
      const optStr  = opts.map((o, i) => (ALPHA[i] || '') + '. ' + (o.text || '')).join(' | ');
      return { QuestionID: rowId, SubQuestion: subLbl, QuestionText: subQTxt, AnswerType: 'MCQ',
               CorrectAnswer: corrLtr + (corrTxt ? '. ' + corrTxt : ''), Marks: rowMarks,
               Options: optStr, ScoringMethod: scoring, Checks: checks, CustomInstructions: custom };
    }

    function _tfRow(rowId, subLbl, subQTxt, tfObj, rowMarks) {
      const c = tfObj.correct;
      return { QuestionID: rowId, SubQuestion: subLbl, QuestionText: subQTxt, AnswerType: 'True / False',
               CorrectAnswer: c === true ? 'True' : c === false ? 'False' : '',
               Marks: rowMarks, Options: 'True | False',
               ScoringMethod: scoring, Checks: checks, CustomInstructions: custom };
    }

    function _textRow(rowId, subLbl, subQTxt, ansText, rowMarks, typeOverride) {
      return { QuestionID: rowId, SubQuestion: subLbl, QuestionText: subQTxt,
               AnswerType: typeOverride || qType,
               CorrectAnswer: ansText, Marks: rowMarks, Options: '',
               ScoringMethod: scoring, Checks: checks, CustomInstructions: custom };
    }

    const raw = ma.modelAnswer || '';
    let parsed = null;
    try { parsed = JSON.parse(raw); } catch (_) {}

    // ── Grouped (sub-questions) ──────────────────────────────────────────
    if (parsed && parsed.__sub__) {
      return parsed.__sub__.map(sub => {
        const subLbl  = (sub.label || '').toUpperCase();
        const subQTxt = sub.question_text || '';
        let subParsed = null;
        try { subParsed = JSON.parse(sub.answer_key || ''); } catch (_) {}

        if (subParsed && subParsed.type === 'multiple_choice') {
          return _mcqRow(qId, subLbl, subQTxt, subParsed, marks);
        }
        if (subParsed && subParsed.type === 'true_false') {
          return _tfRow(qId, subLbl, subQTxt, subParsed, marks);
        }
        // Plain-text sub-question
        const ansText = typeof subParsed === 'string' ? subParsed : (sub.answer_key || '');
        return _textRow(qId, subLbl, subQTxt, ansText, marks);
      });
    }

    // ── MCQ ──────────────────────────────────────────────────────────────
    if (parsed && parsed.type === 'multiple_choice') {
      return [_mcqRow(qId, '', qTxt, parsed, marks)];
    }

    // ── True / False ──────────────────────────────────────────────────────
    if (parsed && parsed.type === 'true_false') {
      return [_tfRow(qId, '', qTxt, parsed, marks)];
    }

    // ── Open-ended / plain text ───────────────────────────────────────────
    return [_textRow(qId, '', qTxt, raw, marks)];
  }

  /** Fetch saved Answer Key from DB and read grading policy from localStorage. */
  async function _getExportData() {
    if (!maCurrentExamId) { toast('Select an exam first.', 'info'); return null; }
    if (!maIsSaved || serverAnswerIds.size === 0) {
      toast('Please save the Answer Key before exporting.', 'info');
      return null;
    }

    let answers;
    try { answers = await apiFetch('/model-answers/?exam_id=' + maCurrentExamId); }
    catch (e) { toast('Failed to fetch Answer Key: ' + e.message, 'error'); return null; }
    if (!answers.length) { toast('No questions in the Answer Key to export.', 'info'); return null; }

    let policy = null;
    try { policy = JSON.parse(localStorage.getItem('taqdeer_gp_' + maCurrentExamId) || 'null'); }
    catch (_) {}
    const policyByQ = {};
    (policy?.question_rules || []).forEach(r => { policyByQ[r.question_number] = r; });

    const chkMap = _buildCheckLabelMap();
    const rows   = [];
    for (const ma of answers) {
      rows.push(..._parseModelAnswerForExport(ma, policyByQ[ma.questionNumber] || {}, chkMap));
    }

    const exam     = exams.find(e => e.id === maCurrentExamId) || {};
    const examName = exam.name || ('Exam ' + maCurrentExamId);
    return { rows, examName, exam };
  }

  /** Download a UTF-8 CSV file. */
  function _exportCSV(rows, examName) {
    function _cell(v) {
      const s = String(v == null ? '' : v);
      return (s.includes(',') || s.includes('"') || s.includes('\n'))
        ? '"' + s.replace(/"/g, '""') + '"'
        : s;
    }
    const hdr  = _EXPORT_COLS.join(',');
    const body = rows.map(r => _EXPORT_COLS.map(c => _cell(r[c] ?? '')).join(',')).join('\n');
    const blob = new Blob(['\uFEFF' + hdr + '\n' + body], { type: 'text/csv;charset=utf-8;' });
    const url  = URL.createObjectURL(blob);
    const a    = Object.assign(document.createElement('a'), {
      href: url, download: (examName + ' - Answer Key.csv').replace(/[/\\?%*:|"<>]/g, '_'),
    });
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }

  /** Download an XLSX file using SheetJS (lazy-loaded). */
  async function _exportXLSX(rows, examName) {
    try { await _loadSheetJS(); }
    catch (e) { toast(e.message, 'error'); return; }

    const { XLSX } = window;
    const aoa = [_EXPORT_COLS, ...rows.map(r => _EXPORT_COLS.map(c => r[c] ?? ''))];
    const ws  = XLSX.utils.aoa_to_sheet(aoa);

    // Bold header row
    _EXPORT_COLS.forEach((_, ci) => {
      const addr = XLSX.utils.encode_cell({ r: 0, c: ci });
      if (ws[addr]) ws[addr].s = { font: { bold: true } };
    });

    // Auto column widths (capped at 60)
    ws['!cols'] = _EXPORT_COLS.map((col, ci) => ({
      wch: Math.min(Math.max(col.length, ...rows.map(r => String(r[col] ?? '').length)) + 2, 60),
    }));

    const wb = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(wb, ws, 'Answer Key');
    XLSX.writeFile(wb, (examName + ' - Answer Key.xlsx').replace(/[/\\?%*:|"<>]/g, '_'));
  }

  /** Open a print-ready window and trigger browser PDF save. */
  function _exportPDF(rows, exam) {
    const examName = exam.name || 'Answer Key';
    const typeMap  = { midterm:'Midterm', final:'Final', quiz:'Quiz', assignment:'Assignment' };
    const examType = typeMap[exam.type] || exam.type || '';
    const examDate = exam.date
      ? new Date(exam.date).toLocaleDateString(undefined, { year:'numeric', month:'long', day:'numeric' })
      : '';
    const semMap   = { first:'First Semester', second:'Second Semester', summer:'Summer Semester' };
    const examSem  = exam.semester ? (semMap[exam.semester] || '') : '';

    function _e(s) {
      return String(s ?? '')
        .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }

    // Group flat rows back into question blocks for hierarchical PDF layout
    const grouped = [];
    let curGroup  = null;
    for (const row of rows) {
      if (!curGroup || row.QuestionID !== curGroup.qId || row.SubQuestion) {
        if (!curGroup || row.QuestionID !== curGroup.qId) {
          curGroup = { qId: row.QuestionID, parts: [] };
          grouped.push(curGroup);
        }
      }
      curGroup.parts.push(row);
    }

    function _partHtml(part) {
      const optBlock = part.Options
        ? `<div class="f-lbl">Options</div><div class="f-opts">`
          + part.Options.split(' | ').map(o => `<div class="f-opt">${_e(o)}</div>`).join('')
          + `</div>`
        : '';
      const chkBlock = part.Checks
        ? `<div class="f-lbl">Checks enabled</div><div class="f-chk">${_e(part.Checks).replace(/; /g,'<br>')}</div>`
        : '';
      const custBlock = part.CustomInstructions
        ? `<div class="f-lbl">Custom instructions</div><div class="f-cust">${_e(part.CustomInstructions)}</div>`
        : '';
      return `
        ${part.QuestionText ? `<div class="f-qtxt">${_e(part.QuestionText)}</div>` : ''}
        <div class="f-meta">
          <span class="f-chip f-type">${_e(part.AnswerType)}</span>
          ${part.Marks !== '' ? `<span class="f-chip f-marks">${_e(String(part.Marks))} marks</span>` : ''}
          ${part.ScoringMethod ? `<span class="f-chip">${_e(part.ScoringMethod)}</span>` : ''}
        </div>
        <div class="f-lbl">Correct Answer</div>
        <div class="f-ans">${part.CorrectAnswer ? _e(part.CorrectAnswer) : '<em style="color:#9ca3af">—</em>'}</div>
        ${optBlock}${chkBlock}${custBlock}`;
    }

    const questionsHtml = grouped.map(grp => {
      const hasSubs = grp.parts.some(p => p.SubQuestion);
      const body = hasSubs
        ? grp.parts.map(p =>
            `<div class="sub-block">
               <div class="sub-lbl">${_e(grp.qId)} — Part ${_e(p.SubQuestion)}</div>
               ${_partHtml(p)}
             </div>`).join('')
        : _partHtml(grp.parts[0]);
      return `<div class="q-card"><div class="q-id">${_e(grp.qId)}</div>${body}</div>`;
    }).join('');

    const html = `<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8">
<title>Answer Key — ${_e(examName)}</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',Arial,sans-serif;font-size:11pt;color:#1e293b;background:#fff;padding:32px 44px 56px}
.hd{border-bottom:2.5px solid #28405b;padding-bottom:16px;margin-bottom:30px}
.hd-title{font-size:22pt;font-weight:800;color:#28405b}
.hd-meta{font-size:10.5pt;color:#6b7280;margin-top:7px;display:flex;gap:18px;flex-wrap:wrap}
.q-card{border:1px solid #d1d5db;border-radius:8px;padding:18px 22px;margin-bottom:18px;page-break-inside:avoid;break-inside:avoid}
.q-id{font-size:14pt;font-weight:800;color:#446b95;margin-bottom:9px}
.f-qtxt{font-size:11pt;color:#1e293b;margin-bottom:10px;line-height:1.55}
.f-meta{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:10px}
.f-chip{display:inline-block;padding:2px 10px;border-radius:999px;font-size:9pt;font-weight:700;background:#f1f5f9;color:#475569;border:1px solid #e2e8f0}
.f-chip.f-type{background:#eff6ff;color:#1d4ed8;border-color:#bfdbfe}
.f-chip.f-marks{background:#f0fdf4;color:#166534;border-color:#bbf7d0}
.f-lbl{font-size:9pt;font-weight:700;color:#6b7280;text-transform:uppercase;letter-spacing:.05em;margin:10px 0 4px}
.f-ans{font-size:11pt;background:#f8fafc;border:1px solid #e2e8f0;border-radius:6px;padding:8px 12px;line-height:1.5}
.f-opts{display:flex;flex-direction:column;gap:3px}
.f-opt{font-size:10.5pt;color:#374151;padding:3px 0}
.f-chk{font-size:10pt;color:#374151;line-height:1.7}
.f-cust{font-size:10pt;color:#374151;font-style:italic;background:#fffbeb;border:1px solid #fde68a;border-radius:6px;padding:7px 10px;margin-top:2px}
.sub-block{border-top:1px solid #e5e7eb;padding-top:13px;margin-top:13px}
.sub-lbl{font-size:11pt;font-weight:700;color:#6895bc;margin-bottom:7px}
.footer{margin-top:32px;border-top:1px solid #e5e7eb;padding-top:12px;font-size:9pt;color:#9ca3af;text-align:center}
@media print{body{padding:16px 24px}.q-card{page-break-inside:avoid;break-inside:avoid}}
</style></head><body>
<div class="hd">
  <div class="hd-title">${_e(examName)}</div>
  <div class="hd-meta">
    ${examType ? `<span>📋 ${_e(examType)}</span>` : ''}
    ${examSem  ? `<span>🗓 ${_e(examSem)}</span>`  : ''}
    ${examDate ? `<span>📅 ${_e(examDate)}</span>` : ''}
    ${exam.totalMarks ? `<span>⭐ Total: ${_e(String(exam.totalMarks))} marks</span>` : ''}
    <span>📄 ${grouped.length} question${grouped.length !== 1 ? 's' : ''}</span>
  </div>
</div>
${questionsHtml}
<div class="footer">Exported from Taqdeer &middot; ${_e(new Date().toLocaleString())}</div>
</body></html>`;

    const w = window.open('', '_blank', 'width=820,height=920,scrollbars=yes');
    if (!w) { toast('Pop-up blocked. Please allow pop-ups and try again.', 'error'); return; }
    w.document.write(html);
    w.document.close();
    w.focus();
    setTimeout(() => w.print(), 650);
  }

  /** Main export entry-point — dispatches to CSV / XLSX / PDF helpers. */
  async function exportAnswerKey(format) {
    document.getElementById('exportWrap')?.classList.remove('open');
    const data = await _getExportData();
    if (!data) return;

    if (format === 'csv') {
      _exportCSV(data.rows, data.examName);
      toast('CSV exported successfully.', 'success');
    } else if (format === 'xlsx') {
      toast('Preparing Excel file…', 'info');
      await _exportXLSX(data.rows, data.examName);
      toast('Excel file exported successfully.', 'success');
    } else if (format === 'pdf') {
      _exportPDF(data.rows, data.exam);
    }
  }

  /* ════════════════════════════════════════════════════════════════════════
     Grading Policy — collect, snapshot (localStorage), and restore
  ════════════════════════════════════════════════════════════════════════ */

  /** Read all grading config from the current DOM into a structured policy object. */
  function collectGradingPolicy(examId) {
    const global_rules = {
      spelling_tolerance:      document.getElementById('gpSpelling')?.value     || 'ignore_unless_meaning_changes',
      language_acceptance:     document.getElementById('gpLanguage')?.value     || 'arabic_and_english',
      partial_credit_strategy: document.getElementById('gpPartialCredit')?.value|| 'proportional',
      handwriting_policy:      document.getElementById('gpHandwriting')?.value  || 'benefit_of_doubt',
      additional_instructions: (document.getElementById('gpAdditional')?.value  || '').trim(),
    };

    const question_rules = [];
    document.querySelectorAll('#maQuestions .ma-question-card').forEach(card => {
      const qNum = parseInt(card.dataset.num, 10);
      if (isNaN(qNum)) return;
      const typeEl    = card.querySelector('[data-gp="type"]');
      const scored    = card.querySelector('[data-gp="scoring"] input[type="radio"]:checked');
      const checkEls  = card.querySelectorAll('[data-gp="checks"] input[type="checkbox"]:checked');
      const customEl  = card.querySelector('[data-gp="custom"]');
      question_rules.push({
        question_number:    qNum,
        question_type:      typeEl?.value    || 'recall_general',
        scoring_method:     scored?.value    || 'keyword_matching',
        checks:             [...checkEls].map(c => c.value),
        custom_instruction: customEl?.value.trim() || '',
      });
    });

    return { exam_id: examId, global_rules, question_rules };
  }

  /** Write the current grading policy to localStorage. */
  function _snapshotGradingPolicy() {
    if (!maCurrentExamId) return;
    try {
      const policy = collectGradingPolicy(maCurrentExamId);
      localStorage.setItem('taqdeer_gp_' + maCurrentExamId, JSON.stringify(policy));
    } catch (e) {}
  }

  /** Restore grading policy from localStorage when an exam is selected. */
  function loadGradingPolicy(examId) {
    // Stage per-question rules so loadModelAnswersForExam can pass them to buildQuestionCard
    window._pendingGPRules = null;
    try {
      const saved = localStorage.getItem('taqdeer_gp_' + examId);
      if (!saved) return;
      const policy = JSON.parse(saved);

      // Restore global rules
      const gr = policy.global_rules || {};
      // Normalise removed language option: fall back to arabic_and_english
      const langVal = gr.language_acceptance === 'mixed_english_arabic'
        ? 'arabic_and_english'
        : gr.language_acceptance;
      const fieldMap = {
        gpSpelling:     gr.spelling_tolerance,
        gpLanguage:     langVal,
        gpPartialCredit:gr.partial_credit_strategy,
        gpHandwriting:  gr.handwriting_policy,
        gpAdditional:   gr.additional_instructions,
      };
      Object.entries(fieldMap).forEach(([id, val]) => {
        const el = document.getElementById(id);
        if (el && val !== undefined) el.value = val;
      });

      // Index per-question rules by question_number for fast card lookup
      const rules = {};
      (policy.question_rules || []).forEach(qr => { rules[qr.question_number] = qr; });
      window._pendingGPRules = rules;
    } catch (e) {}
  }

  // "Save Grading Instructions" button — saves global + per-question policy to localStorage only
  document.getElementById('btnSaveGradingPolicy').addEventListener('click', () => {
    if (!maCurrentExamId) { toast('Select an exam first.', 'info'); return; }
    _snapshotGradingPolicy();
    const badge = document.getElementById('gpSavedBadge');
    if (badge) {
      badge.classList.add('show');
      setTimeout(() => badge.classList.remove('show'), 3000);
    }
    toast('Grading instructions saved.', 'success');
  });

  /* ── Exam dropdown change ───────────────────────────────────────────────── */
  document.getElementById('fExam').addEventListener('change', async function () {
    console.trace('[RUBRICS-TRACE] fExam change event — value=' + this.value + ' _akExtractionActive=' + _akExtractionActive);
    clearErr('fExam');
    this.classList.remove('invalid');
    const examId = parseInt(this.value, 10);
    if (examId) localStorage.setItem('currentExamId', examId);

    // If extraction is active for a different exam, auto-cancel it silently.
    if (typeof window._akExamChanged === 'function') window._akExamChanged(examId || null);

    // Do not render empty state while answer key extraction is active.
    // This prevents UI regression where progress strip disappears during background extraction.
    // updateModelAnswerSection is also guarded, but the explicit check here
    // prevents the rubric progress + workflow state from being reset mid-extraction.
    if (_akExtractionActive) return;

    if (!examId) {
      currentWf = null;
      renderProgress(null);
      updateRubricNotice(null);
      await updateModelAnswerSection(null);
      return;
    }
    const wf = wfFromExam(examId);
    currentWf = wf;
    renderProgress(wf);
    updateRubricNotice(wf);
    await updateModelAnswerSection(examId);
  });

  /* ── Answer Key button listeners ────────────────────────────────────────── */
  document.getElementById('btnAddQuestion').addEventListener('click', addModelQuestion);
  document.getElementById('btnSaveModelAnswers').addEventListener('click', saveCurrentModelAnswers);

  /* ── Export dropdown ─────────────────────────────────────────────────────── */
  document.getElementById('btnExportTrigger').addEventListener('click', e => {
    e.stopPropagation();
    document.getElementById('exportWrap').classList.toggle('open');
  });
  document.getElementById('exportDropdown').addEventListener('click', e => {
    const btn = e.target.closest('[data-export]');
    if (btn) exportAnswerKey(btn.dataset.export);
  });
  // Close dropdown when clicking anywhere outside
  document.addEventListener('click', () => {
    document.getElementById('exportWrap')?.classList.remove('open');
  });

  /* ── Extract Answer Key from File ───────────────────────────────────────── */
  (function _initAkExtract() {
    const btn       = document.getElementById('btnAkExtract');
    const input     = document.getElementById('akExtractFileInput');
    const strip     = document.getElementById('akExtractStrip');
    const btnSpinner = document.getElementById('akUploadSpinner');
    const btnText   = document.getElementById('akBtnText');
    const overlay   = document.getElementById('akExtractionOverlay');
    const ovTitle   = document.getElementById('akOvTitle');
    const ovSub     = document.getElementById('akOvSub');
    if (!btn || !input || !strip) return;

    // ── State machine ─────────────────────────────────────────────────────
    // States: idle-empty | uploading | extracting | success | error | loaded
    let _akState = 'idle-empty';
    let _pollId  = null;

    function _currentExamId() {
      return parseInt(document.getElementById('fExam')?.value, 10) || null;
    }

    // ── Strip (compact banner above the editor) ───────────────────────────
    const _ICON_OK  = '<svg viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></svg>';
    const _ICON_ERR = '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>';

    function _strip(cls, icon, msg) {
      strip.className = 'ak-extract-strip' + (cls ? ' ' + cls : '');
      strip.innerHTML = (icon === 'spinner' ? '<div class="ak-strip-spinner"></div>' : icon)
        + '<span>' + msg + '</span>';
      strip.style.display = 'flex';
    }

    // ── Button loading helpers ────────────────────────────────────────────
    function _setBtnLoading(label) {
      btn.disabled = true;
      if (btnText) btnText.textContent = label;
      if (btnSpinner) btnSpinner.style.display = 'inline-block';
    }
    function _setBtnIdle() {
      btn.disabled = false;
      if (btnText) btnText.textContent = 'Upload Answer Key';
      if (btnSpinner) btnSpinner.style.display = 'none';
    }

    // ── Phase transitions ─────────────────────────────────────────────────
    // The overlay (absolute-positioned, zero layout impact) and strip provide
    // all visual feedback.  #maQuestions card content is never touched until
    // _renderExtractedAnswers runs at the very end of a successful extraction.

    function _showOverlay(title, sub) {
      if (ovTitle) ovTitle.textContent = title;
      if (ovSub)   ovSub.textContent   = sub;
      if (overlay) overlay.style.display = '';
    }
    function _hideOverlay() {
      if (overlay) overlay.style.display = 'none';
    }

    function _enterUploading(filename) {
      _akState = 'uploading';
      _akExtractionActive = true;   // block loadModelAnswersForExam until done
      _setBtnLoading('Uploading\u2026');
      _showOverlay('Uploading answer key\u2026', '\u201c' + filename + '\u201d');
      _strip('', 'spinner', 'Uploading \u201c' + filename + '\u201d\u2026');
      console.log('[AK] before upload — scroll:', (document.querySelector('main')||document.documentElement).scrollTop, 'visibility:', document.visibilityState);
    }

    function _enterExtracting(elapsed) {
      _akState = 'extracting';
      const lbl  = elapsed === 0 ? 'Extracting answer key\u2026'
        : 'Extracting answer key\u2026 (' + elapsed + 's)';
      if (elapsed === 0) {
        _setBtnLoading('Extracting\u2026');
        _showOverlay('Extracting answer key from PDF\u2026', 'This may take a few minutes \u2014 please keep this page open');
        console.log('[AK] extraction started — scroll:', (document.querySelector('main')||document.documentElement).scrollTop, 'visibility:', document.visibilityState);
      } else {
        // Update elapsed counter in overlay subtitle
        if (ovSub) ovSub.textContent = 'Processing\u2026 ' + elapsed + 's elapsed \u2014 please keep this page open';
      }
      _strip('', 'spinner', lbl);
    }

    function _enterSuccess(count) {
      _akState = 'success';
      const noun = count + ' question' + (count !== 1 ? 's' : '');
      _showOverlay('Extraction complete \u2014 ' + noun + ' extracted', 'Loading questions into editor\u2026');
      _strip('strip-success', _ICON_OK, 'Extraction complete \u2014 ' + noun + ' loaded.');
      _setBtnIdle();
      toast('Answer key extracted successfully.', 'success');
    }

    function _enterError(msg) {
      _akState = 'error';
      _akExtractionActive = false;  // release lock so exam-change can work again
      _hideOverlay();
      _strip('strip-error', _ICON_ERR, msg);
      _setBtnIdle();
      toast('Extraction failed: ' + msg, 'error');
      if (typeof window._akBannerHide === 'function') window._akBannerHide(_currentExamId());
    }

    // ── Render extraction results in-place (no second network fetch) ──────
    // Called from the success path using the rows already returned by the poll,
    // avoiding a duplicate GET /model-answers request and its race-condition window.
    async function _renderExtractedAnswers(rows, examId) {
      const mainEl = /** @type {HTMLElement|null} */ (document.querySelector('main'));
      const savedScroll = mainEl ? mainEl.scrollTop : window.scrollY;
      console.log('[AK] rendering extracted answers — rows:', rows.length, 'scroll:', savedScroll, 'visibility:', document.visibilityState);

      const container = document.getElementById('maQuestions');
      if (!container) return;

      // Ungating the section
      const section = document.getElementById('modelAnswerSection');
      if (section) section.classList.remove('ak-gated');
      maCurrentExamId = examId;
      hideMaSavedBadge();
      maIsSaved       = false;
      serverAnswerIds = new Set();
      container.innerHTML = '';

      loadGradingPolicy(examId);

      rows.forEach(a => {
        const gpCfg = (window._pendingGPRules || {})[a.questionNumber] || {};
        let subQs = null;
        let plainAnswer = a.modelAnswer;
        if (a.modelAnswer && a.modelAnswer.startsWith('{"__sub__":')) {
          try {
            const parsed = JSON.parse(a.modelAnswer);
            if (Array.isArray(parsed.__sub__)) { subQs = parsed.__sub__; plainAnswer = ''; }
          } catch (_) {}
        }
        const card = buildQuestionCard(a.questionNumber, {
          answerId:          a.id,
          questionText:      a.questionText,
          modelAnswer:       plainAnswer,
          sub_questions:     subQs,
          score:             a.score,
          questionType:      a.questionType || gpCfg.question_type,
          scoringMethod:     gpCfg.scoring_method,
          checks:            gpCfg.checks,
          customInstruction: gpCfg.custom_instruction,
        });
        _restoreCardImages(card, examId, a.id);
        card.dataset.navStatus = 'saved';
        container.appendChild(card);
        serverAnswerIds.add(a.id);
      });
      window._pendingGPRules = null;
      maIsSaved = true;
      showMaSavedBadge();
      updateMaCount();
      updateNextBtn();

      // Hide the extraction overlay — cards are rendered in place
      _hideOverlay();

      // Release the extraction lock AFTER cards are painted and overlay is gone.
      // Keeping _akExtractionActive=true until this point ensures that any
      // concurrent loadModelAnswersForExam call cannot fetch 0 rows and blank the UI.
      _akExtractionActive = false;
      if (typeof window._akBannerHide === 'function') window._akBannerHide(examId);
      console.log('[AK] extraction lock released — cards rendered, _akExtractionActive=false');

      // Restore scroll position — rebuilding the card list changes content
      // height transiently; rAF fires after the browser has laid out the new
      // cards so the target scroll position is actually reachable.
      requestAnimationFrame(() => {
        if (mainEl) mainEl.scrollTop = savedScroll;
        else window.scrollTo(0, savedScroll);
        console.log('[AK] scroll restored to', savedScroll);
      });
    }

    // Scroll position to restore when the file picker closes (some browsers
    // reset overflow:auto scroll containers when the window loses focus).
    let _savedScrollForPicker = 0;

    // ── Click: open file picker ────────────────────────────────────────────────
    // Re-extraction is explicitly allowed. We only block if a transfer is already
    // in-flight (_akState === 'uploading' | 'extracting').
    //
    // IMPORTANT: _akExtractionActive is set TRUE here — immediately on click,
    // before the file picker even opens.  This closes the race window between
    // "user clicks button" and "user selects file" during which any concurrent
    // loadModelAnswersForExam call could run, get 0 rows (backend already
    // deleted old rows synchronously), and render a false empty state.
    //
    // If the user dismisses the picker without selecting a file, the 'change'
    // event will NOT fire on most browsers.  We use a one-shot window 'focus'
    // listener to detect picker dismissal and release the lock when no
    // extraction has actually started.
    btn.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (_akState === 'uploading' || _akState === 'extracting') return;
      const examId = _currentExamId();
      if (!examId) { toast('Select an exam first.', 'info'); return; }

      // Lock NOW — before the file picker steals focus.
      _akExtractionActive = true;
      console.log('[AK] picker lock acquired — _akExtractionActive=true');

      // Save scroll before opening the picker.
      const _mainEl = document.querySelector('main');
      _savedScrollForPicker = _mainEl ? _mainEl.scrollTop : window.scrollY;

      input.value = '';
      input.click();

      // One-shot focus listener: detects picker dismissal (cancel / close without
      // selecting).  Fires when the browser window regains focus after the dialog
      // closes.  The 300 ms delay lets the 'change' event fire first if the user
      // DID select a file (in that case _akState will already be 'uploading').
      const _onPickerDismiss = () => {
        window.removeEventListener('focus', _onPickerDismiss);
        setTimeout(() => {
          // If _enterUploading was already called, _akState is 'uploading' or
          // 'extracting' — leave the lock in place.
          if (_akState !== 'uploading' && _akState !== 'extracting') {
            _akExtractionActive = false;
            console.log('[AK] picker dismissed without selection — lock released');
          }
        }, 300);
      };
      window.addEventListener('focus', _onPickerDismiss);
    });

    // ── File chosen: upload → poll loop ───────────────────────────────────
    input.addEventListener('change', async (e) => {
      e.preventDefault();
      e.stopPropagation();

      // Immediately restore any scroll position the file-picker dialog may have
      // reset when it stole focus from the browser window.
      if (_savedScrollForPicker > 0) {
        const _mainElRestore = document.querySelector('main');
        if (_mainElRestore) _mainElRestore.scrollTop = _savedScrollForPicker;
        else window.scrollTo(0, _savedScrollForPicker);
      }

      const file = input.files?.[0];
      if (!file) {
        // Picker closed with no file (some browsers fire 'change' with empty
        // files on cancel).  Release the lock acquired in the click handler.
        if (_akState !== 'uploading' && _akState !== 'extracting') {
          _akExtractionActive = false;
          console.log('[AK] change fired with no file — lock released');
        }
        return;
      }
      const examId = _currentExamId();
      if (!examId) {
        // No exam selected — release lock and bail.
        _akExtractionActive = false;
        return;
      }

      const ext = file.name.split('.').pop().toLowerCase();
      if (!['pdf', 'docx'].includes(ext)) {
        toast('Please select a PDF or DOCX file.', 'error');
        _akExtractionActive = false;   // release lock — no upload will happen
        return;
      }

      // Cancel any prior poll
      if (_pollId) { clearInterval(_pollId); _pollId = null; }

      console.log('[ANSWER-KEY-EXTRACT] started — examId=' + examId + ' file=' + file.name);

      // btnSpinner is managed by _setBtnLoading/_setBtnIdle via phase transitions.
      // Explicitly hide it here as a safety reset before _enterUploading takes over.
      if (btnSpinner) btnSpinner.style.display = 'none';

      // ── Phase A: uploading ─────────────────────────────────────────────
      // Show the body-level global banner FIRST — it survives any subsequent
      // page re-render and is written to sessionStorage so it auto-restores
      // if the page accidentally reloads mid-extraction.
      if (typeof window._akBannerShow === 'function') window._akBannerShow(examId);

      // _enterUploading shows the in-section overlay over whatever cards are
      // currently in #maQuestions.  It does NOT clear the container.
      _enterUploading(file.name);

      try {
        const fd = new FormData();
        fd.append('file', file);
        await apiFetch('/extraction/answer-key/' + examId, { method: 'POST', body: fd });
      } catch (err) {
        // ── Phase E: HTTP error (4xx/5xx) — server explicitly rejected ────
        // TypeError means a network/transport failure: the server may have
        // already accepted the file and queued extraction as a background
        // task.  In that case do NOT stop here — fall through to polling so
        // the UI auto-recovers when results appear.
        if (!(err instanceof TypeError)) {
          _enterError(err.message || 'Upload failed.');
          return;
        }
        // Network error: stay in extracting state and let the poll decide.
      }

      // ── Phase B: 202 accepted (or transport error — server may have
      //    accepted) → 15%.  Progress component now owns #maQuestions.
      let elapsed = 0;
      _enterExtracting(elapsed);

      // ── Phase C: poll every 5 s, advance simulated progress ──────────
      _pollId = setInterval(async () => {
        elapsed += 5;
        _enterExtracting(elapsed);   // updates bar and strip on every tick

        // Timeout guard (5 min)
        if (elapsed >= 300) {
          clearInterval(_pollId); _pollId = null;
          _enterError('Extraction is taking longer than expected. Check server logs.');
          return;
        }

        try {
          const rows = await apiFetch('/model-answers/?exam_id=' + examId);
          if (Array.isArray(rows) && rows.length > 0) {
            clearInterval(_pollId); _pollId = null;

            // ── Phase D: questions arrived → success ───────────────────
            console.log('[AK] poll completed —', rows.length, 'question(s) detected, scroll:', (document.querySelector('main')||{scrollTop:window.scrollY}).scrollTop, 'visibility:', document.visibilityState);
            _enterSuccess(rows.length);

            // Brief pause so the success strip is visible, then render cards
            // in-place using the rows already returned by this poll.
            // _akExtractionActive is intentionally kept TRUE here and released
            // only inside _renderExtractedAnswers, AFTER cards are fully painted.
            // This prevents any concurrent loadModelAnswersForExam call that
            // arrives during the render from seeing _akExtractionActive=false,
            // fetching 0 rows (backend already cleared old ones), and blanking the UI.
            setTimeout(async () => {
              _akState = 'loaded';
              await _renderExtractedAnswers(rows, examId);
              // _akExtractionActive = false is set at the END of _renderExtractedAnswers
            }, 700);
          }
          // No rows yet — keep polling; extraction still running
        } catch (_) { /* network hiccup — keep polling */ }
      }, 5000);
    });
  })();

  /* ── Exam Summary Panel wiring ──────────────────────────────────────────── */
  // Nav button toggle
  document.getElementById('maSummaryNavBtn')?.addEventListener('click', e => {
    e.stopPropagation();
    const btn      = document.getElementById('maSummaryNavBtn');
    const dropdown = document.getElementById('maSummaryNavDropdown');
    const isOpen   = dropdown.classList.contains('open');
    if (isOpen) {
      _closeSummaryNav();
    } else {
      // Refresh nav items before opening
      updateExamSummary();
      dropdown.classList.add('open');
      btn.setAttribute('aria-expanded', 'true');
      _positionNavDropdown();
    }
  });

  // ── Portal: move the nav dropdown to <body> so it escapes every
  //    overflow/backdrop-filter containing block inside .app ──────────────────
  (function _portalNavDropdown() {
    const dropdown = document.getElementById('maSummaryNavDropdown');
    if (dropdown) document.body.appendChild(dropdown);
  })();

  // Close nav dropdown on outside click (must check both trigger AND the portalled dropdown)
  document.addEventListener('click', e => {
    const trigger  = document.getElementById('maSummaryQNav');
    const dropdown = document.getElementById('maSummaryNavDropdown');
    const clickedInsideTrigger  = trigger  && trigger.contains(e.target);
    const clickedInsideDropdown = dropdown && dropdown.contains(e.target);
    if (!clickedInsideTrigger && !clickedInsideDropdown) _closeSummaryNav();
  });

  // Update active box while scrolling (only when dropdown is open)
  document.querySelector('main')?.addEventListener('scroll', () => {
    if (document.getElementById('maSummaryNavDropdown')?.classList.contains('open')) {
      _updateNavActiveBox();
    }
  }, { passive: true });

  // Reposition dropdown if window resizes while it's open
  window.addEventListener('resize', () => {
    if (document.getElementById('maSummaryNavDropdown')?.classList.contains('open')) {
      _positionNavDropdown();
    }
  }, { passive: true });

  // Delegated listener on #maQuestions — tracks per-card dirty status + live summary updates
  document.getElementById('maQuestions')?.addEventListener('input', e => {
    // Mark the containing card as edited
    const card = e.target.closest('.ma-question-card');
    if (card && card.dataset.navStatus !== 'edited') {
      card.dataset.navStatus = 'edited';
      // Refresh just the status of this box in the open dropdown (avoid full rebuild)
      const idx   = [...document.querySelectorAll('#maQuestions .ma-question-card')].indexOf(card);
      const boxes = document.querySelectorAll('#maSummaryNavDropdown .ma-nav-box');
      if (boxes[idx]) boxes[idx].dataset.status = 'edited';
    }
    // Also refresh marks total when a score input changes
    if (e.target.classList.contains('ma-score-inline-input')) {
      updateExamSummary();
    }
  });
  // Same for change events (select elements inside cards)
  document.getElementById('maQuestions')?.addEventListener('change', e => {
    const card = e.target.closest('.ma-question-card');
    if (card && card.dataset.navStatus !== 'edited') {
      card.dataset.navStatus = 'edited';
      const idx   = [...document.querySelectorAll('#maQuestions .ma-question-card')].indexOf(card);
      const boxes = document.querySelectorAll('#maSummaryNavDropdown .ma-nav-box');
      if (boxes[idx]) boxes[idx].dataset.status = 'edited';
    }
  });

  /* ════════════════════════════════════════════════════════════════════════
     Toast
  ════════════════════════════════════════════════════════════════════════ */
  const T_ICONS = {
    success: '<svg viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></svg>',
    error:   '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>',
    info:    '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12.01" y2="16"/><line x1="12" y1="8" x2="12" y2="12"/></svg>',
  };
  let toastTmr;
  function toast(msg, type) {
    type     = type || 'success';
    const el = document.getElementById('toast');
    document.getElementById('toastMsg').textContent = msg;
    document.getElementById('toastIcon').innerHTML  = T_ICONS[type] || T_ICONS.info;
    el.className = 'toast ' + type + ' show';
    clearTimeout(toastTmr);
    toastTmr = setTimeout(() => el.classList.remove('show'), 3500);
  }

  /* ════════════════════════════════════════════════════════════════════════
     Sidebar / logout / keyboard
  ════════════════════════════════════════════════════════════════════════ */
  const sb  = document.getElementById('sidebar');
  const sbo = document.getElementById('sidebarOverlay');
  document.getElementById('sidebarToggle').addEventListener('click', () => { sb.classList.toggle('mobile-open'); sbo.classList.toggle('open'); });
  sbo.addEventListener('click', () => { sb.classList.remove('mobile-open'); sbo.classList.remove('open'); });

  const logoutOvl = document.getElementById('logoutModal');
  document.getElementById('logoutTrigger').addEventListener('click', e => { e.preventDefault(); logoutOvl.classList.add('active'); });
  document.getElementById('logoutCancelBtn').addEventListener('click', () => logoutOvl.classList.remove('active'));
  logoutOvl.addEventListener('click', e => { if (e.target === logoutOvl) logoutOvl.classList.remove('active'); });

  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    document.getElementById('deleteModal').classList.remove('open');
    logoutOvl.classList.remove('active');
    deleteId = null;
  });

  /* ════════════════════════════════════════════════════════════════════════
     General Grading Instructions — collapsible toggle
  ════════════════════════════════════════════════════════════════════════ */
  (function () {
    const section = document.getElementById('gradingPolicySection');
    const toggle  = document.getElementById('gradingPolicyToggle');
    if (!section || !toggle) return;

    toggle.addEventListener('click', () => {
      const collapsed = section.classList.toggle('collapsed');
      toggle.setAttribute('aria-expanded', String(!collapsed));
    });
  })();

  /* ════════════════════════════════════════════════════════════════════════
     Init
  ════════════════════════════════════════════════════════════════════════ */
  initUserChip();
  await loadExams();
  await loadRubrics();
  populateExamDropdown();
  renderTable();
  renderProgress(null);

  // Auto-select exam from URL param (?examId=) or last-used exam
  (function () {
    const urlId   = new URLSearchParams(window.location.search).get('examId');
    const savedId = parseInt(urlId || localStorage.getItem('currentExamId'), 10);
    if (!isNaN(savedId)) {
      const sel   = document.getElementById('fExam');
      const found = Array.from(sel.options).some(o => parseInt(o.value, 10) === savedId);
      if (found) { sel.value = savedId; sel.dispatchEvent(new Event('change')); }
    }
  }());

}()); // end async IIFE

} // end else (!window._rubricsInitDone)
