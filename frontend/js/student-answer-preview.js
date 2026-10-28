authGuard();
(async function () {
  'use strict';

  /* ── Helpers ─────────────────────────────────────────────────────────────── */
  function esc(str) {
    return String(str).replace(/[&"'<>]/g, c => ({'&':'&amp;','"':'&quot;',"'":'&#39;','<':'&lt;','>':'&gt;'}[c]));
  }
  function fmtDate(iso) {
    try { return new Date(iso).toLocaleDateString(undefined, { month: 'long', day: 'numeric', year: 'numeric' }); }
    catch { return iso || ''; }
  }
  function showError(title, detail) {
    document.getElementById('pvHeaderBody').innerHTML =
      '<div class="pv-status">'
      + '<div class="pv-error-ring"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/>'
      + '<line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg></div>'
      + '<div class="pv-error-msg">' + esc(title) + '</div>'
      + (detail ? '<div style="margin-top:6px;font-size:12px;color:var(--text-muted);">' + esc(detail) + '</div>' : '')
      + '</div>';
  }

  /* ── URL params ──────────────────────────────────────────────────────────── */
  const params       = new URLSearchParams(window.location.search);
  const examId       = params.get('examId');
  const extractionId = params.get('extractionId');
  if (!examId || !extractionId) {
    showError('Missing parameters', 'examId and extractionId are required in the URL.');
    return;
  }

  /* ── Fetch all data in parallel ──────────────────────────────────────────── */
  let exam, extractionData, modelAnswers;
  try {
    [exam, extractionData, modelAnswers] = await Promise.all([
      apiFetch('/exams/' + examId),
      apiFetch(`/extraction/students/${examId}/${extractionId}/answers`),
      apiFetch(`/model-answers/?exam_id=${examId}`).catch(() => []),
    ]);
  } catch (err) {
    showError('Failed to load preview data', err.message || 'Unknown error');
    return;
  }

  const extraction = extractionData.extraction;
  const answers    = extractionData.answers || [];

  /* ── Render header card ──────────────────────────────────────────────────── */
  const studentName    = extraction.studentName    || null;
  const studentIdNum   = extraction.studentIdNum   || null;
  const studentSection = extraction.studentSection || null;
  const initials = studentName
    ? studentName.split(' ').filter(Boolean).map(w => w[0]).join('').slice(0, 2).toUpperCase()
    : '?';
  const typeLabels = { midterm: 'Midterm', final: 'Final', quiz: 'Quiz', assignment: 'Assignment' };
  const examTypeLabel = typeLabels[exam.type] || exam.type || '';
  let stuInfoHtml;
  if (!studentName && !studentIdNum) {
    stuInfoHtml = '<span class="pvh-unknown">Student information not extracted</span>';
  } else {
    stuInfoHtml =
      (studentName ? `<div class="pvh-name">${esc(studentName)}</div>` : '')
      + '<div class="pvh-uid-row">'
      + (studentIdNum   ? `<span class="pvh-uid-badge">${esc(studentIdNum)}</span>` : '')
      + (studentSection ? `<span class="pvh-section-badge">Section ${esc(studentSection)}</span>` : '')
      + '</div>';
  }
  document.getElementById('pvHeaderBody').innerHTML = `
    <div class="pvh-grid">
      <div class="pvh-student">
        <div class="pvh-avatar">${esc(initials)}</div>
        <div>${stuInfoHtml}</div>
      </div>
      <div class="pvh-exam">
        <div class="pvh-exam-label">Exam</div>
        <div class="pvh-exam-name">${esc(exam.name)}</div>
        <div class="pvh-exam-meta">
          ${examTypeLabel ? `<span class="pvh-meta-chip"><svg viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>${esc(examTypeLabel)}</span>` : ''}
          ${exam.date ? `<span class="pvh-meta-chip"><svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="18" rx="2" ry="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>${esc(fmtDate(exam.date))}</span>` : ''}
          ${exam.section ? `<span class="pvh-meta-chip"><svg viewBox="0 0 24 24"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>Section ${esc(exam.section)}</span>` : ''}
          ${exam.totalMarks ? `<span class="pvh-marks-chip">${esc(String(exam.totalMarks))} marks</span>` : ''}
        </div>
      </div>
    </div>
  `;

  /* ── Render answer cards ─────────────────────────────────────────────────── */
  document.getElementById('pvAnswerCard').style.display = '';
  if (!answers.length) {
    document.getElementById('pvAnswerBody').innerHTML =
      '<div class="sa-no-answers">No answers were extracted for this student.</div>';
    return;
  }

  /* ── Mutable session state ───────────────────────────────────────────────── */
  // Session-only MCQ option text overrides: mcqKey → { 'a': 'text', 'b': 'text', ... }
  // These do NOT persist after page refresh — selection changes do (via DB PATCH).
  const _mcqOptTextOverrides = {};
  // Tracks DB-persisted item IDs (for modified badge on re-load within session)
  const _modifiedItems = new Set();

  /* ── Rendering helpers ───────────────────────────────────────────────────── */
  function _parseQId(qid) {
    let m = qid.match(/^(Q\d+)([a-zA-Z]+)$/i);
    if (m) return { parent: m[1].toUpperCase(), sub: m[2].toLowerCase() };
    m = qid.match(/^(Q\d+)\.(\d+)$/i);
    if (m) return { parent: m[1].toUpperCase(), sub: m[2] };
    return { parent: qid.toUpperCase(), sub: null };
  }

  function _groupAnswerItems(items) {
    const groups = new Map();
    const order  = [];
    for (const item of items) {
      const { parent, sub } = _parseQId(item.questionId || '');
      if (!groups.has(parent)) { groups.set(parent, { parent, items: [] }); order.push(parent); }
      groups.get(parent).items.push({ ...item, _sub: sub });
    }
    return order.map(p => groups.get(p));
  }

  function _buildQTextLookup(mas) {
    const lu = {};
    for (const ma of mas) {
      const qkey = 'Q' + ma.questionNumber;
      lu[qkey] = ma.questionText || '';
      const raw = ma.modelAnswer || '';
      if (raw.startsWith('{"__sub__":')) {
        try {
          const parsed = JSON.parse(raw);
          for (const entry of (parsed.__sub__ || [])) {
            lu[qkey + (entry.label || '').toLowerCase()] = entry.question_text || '';
          }
        } catch (_) {}
      }
    }
    return lu;
  }

  function _buildMcqLookup(mas) {
    const lu = {};
    for (const ma of mas) {
      const qkey = 'Q' + ma.questionNumber;
      const raw  = ma.modelAnswer || '';
      if (raw.startsWith('{"__sub__":')) {
        try {
          const parsed = JSON.parse(raw);
          for (const entry of (parsed.__sub__ || [])) {
            const subKey = qkey + (entry.label || '').toLowerCase();
            try {
              const ak = JSON.parse(entry.answer_key || '');
              if (ak && Array.isArray(ak.options)) lu[subKey] = ak.options;
            } catch (_) {}
          }
        } catch (_) {}
      } else {
        try {
          const parsed = JSON.parse(raw);
          if (parsed && Array.isArray(parsed.options)) lu[qkey] = parsed.options;
        } catch (_) {}
      }
    }
    return lu;
  }

  function _drawingUrl(filePath) {
    if (!filePath) return null;
    const normalized = filePath.replace(/\\/g, '/');
    const idx = normalized.indexOf('uploads/');
    if (idx === -1) return null;
    return API_BASE + '/' + normalized.slice(idx);
  }

  /**
   * Render MCQ options as DOM nodes.
   * @param {string|null} mcqMarksStr  - JSON string like {"a":"SELECTED","b":"CROSSED_OUT"}
   * @param {string|null} selectedOpt  - fallback selected letter when marks absent
   * @param {string|null} correctOpt   - the answer key's correct letter
   * @param {Array|null}  options      - model answer options array [{text, correct}, ...]
   * @param {Object|null} sessionOptText - session override map letter→text
   */
  function _renderMcqOpts(mcqMarksStr, selectedOpt, correctOpt, options, sessionOptText) {
    let marks = {};
    try { if (mcqMarksStr) marks = JSON.parse(mcqMarksStr); } catch (_) {}

    // Build option text: model answers → then session overrides on top
    const optText = {};
    if (Array.isArray(options)) {
      const letters = 'abcdefghij';
      options.forEach((opt, i) => { if (i < letters.length) optText[letters[i]] = opt.text || ''; });
    }
    if (sessionOptText) Object.assign(optText, sessionOptText);

    if (!Object.keys(marks).length) {
      const sel = (selectedOpt || '').trim().toLowerCase();
      if (sel) {
        const txt = optText[sel] || '';
        const wrap = document.createElement('div');
        wrap.className = 'sa-mcq-opts';
        const opt = document.createElement('div');
        opt.className = 'sa-mcq-opt sa-selected';
        opt.innerHTML = '<span class="sa-mcq-mark">✓</span><span>'
          + esc(sel.toUpperCase()) + (txt ? '. ' + esc(txt) : ' — selected') + '</span>';
        wrap.appendChild(opt);
        return wrap;
      }
      const blank = document.createElement('div');
      blank.className = 'sa-answer-text sa-blank';
      blank.textContent = '(no selection recorded)';
      return blank;
    }

    const container = document.createElement('div');
    container.className = 'sa-mcq-opts';
    for (const [letter, state] of Object.entries(marks).sort()) {
      const isCorrect  = correctOpt && letter.toLowerCase() === (correctOpt || '').toLowerCase();
      const isSelected = state === 'SELECTED';
      const isCrossed  = state === 'CROSSED_OUT';
      let cls = 'sa-mcq-opt';
      if (isSelected) cls += ' sa-selected';
      if (isCrossed)  cls += ' sa-crossed';
      if (isCorrect)  cls += ' sa-correct-key';
      const mark = isSelected ? '✓' : isCrossed ? '✗' : '·';
      const txt  = optText[letter.toLowerCase()] || '';
      const opt  = document.createElement('div');
      opt.className = cls;
      opt.innerHTML = `<span class="sa-mcq-mark">${mark}</span>`
        + `<span>${esc(letter.toUpperCase())}${txt ? '. ' + esc(txt) : ''}${isCorrect ? ' ★' : ''}</span>`;
      container.appendChild(opt);
    }
    return container;
  }

  /**
   * Build the options list for the MCQ edit form.
   * Sources (in priority order): session overrides → model answers → mcqOptionMarks keys → A/B/C/D.
   */
  function _getMcqEditOptions(item, mcqLookup, mcqKey) {
    const modelOptions    = mcqLookup ? (mcqLookup[mcqKey] || null) : null;
    const sessionOverride = _mcqOptTextOverrides[mcqKey] || {};

    if (modelOptions && modelOptions.length) {
      const letters = 'abcdefghij';
      return modelOptions.map((opt, i) => {
        const letter = letters[i] || String.fromCharCode(97 + i);
        return {
          letter,
          text: sessionOverride[letter] !== undefined ? sessionOverride[letter] : (opt.text || ''),
        };
      });
    }

    // Fall back to keys in mcqOptionMarks
    let marks = {};
    try { if (item.mcqOptionMarks) marks = JSON.parse(item.mcqOptionMarks); } catch (_) {}
    const markLetters = Object.keys(marks).sort();
    if (markLetters.length) {
      return markLetters.map(l => ({
        letter: l.toLowerCase(),
        text: sessionOverride[l.toLowerCase()] || '',
      }));
    }

    // Final fallback: A B C D
    return ['a','b','c','d'].map(letter => ({
      letter,
      text: sessionOverride[letter] || '',
    }));
  }

  /* ── SVG icon helpers ────────────────────────────────────────────────────── */
  function _svgEdit()   { return '<svg viewBox="0 0 24 24"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>'; }
  function _svgTrash()  { return '<svg viewBox="0 0 24 24"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4h6v2"/></svg>'; }
  function _svgSave()   { return '<svg viewBox="0 0 24 24"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>'; }
  function _svgCancel() { return '<svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>'; }

  /* ── Build read content fragment ─────────────────────────────────────────── */
  function _buildAnswerContentEl(item, mcqLookup) {
    const frag  = document.createDocumentFragment();
    const atype = (item.answerType || '').toLowerCase();
    const isMcq = atype === 'multiple_choice' || atype === 'true_false';

    const answerLabel = document.createElement('div');
    answerLabel.className = 'sa-answer-label';
    answerLabel.textContent = isMcq ? 'Student Selection' : 'Student Answer';
    frag.appendChild(answerLabel);

    if (isMcq) {
      const { parent, sub } = _parseQId(item.questionId || '');
      const mcqKey    = parent + (sub || '');
      const options   = mcqLookup ? (mcqLookup[mcqKey] || null) : null;
      const sessionTx = _mcqOptTextOverrides[mcqKey] || null;
      frag.appendChild(_renderMcqOpts(item.mcqOptionMarks, item.studentSelectedOption, item.mcqKeyCorrectOption, options, sessionTx));
    } else {
      const ans = (item.studentAnswer || '').trim();
      const ansEl = document.createElement('div');
      ansEl.className = 'sa-answer-text' + (ans ? '' : ' sa-blank');
      ansEl.textContent = ans || '(blank)';
      frag.appendChild(ansEl);
    }

    // Drawing
    if (item.hasDrawing) {
      const imgUrl = _drawingUrl(item.drawingFilePath);
      if (imgUrl) {
        const wrap = document.createElement('div');
        wrap.className = 'sa-drawing-img-wrap';
        const img = document.createElement('img');
        img.className = 'sa-drawing-img';
        img.src = imgUrl;
        img.alt = 'Student drawing for ' + (item.questionId || 'question');
        img.loading = 'lazy';
        wrap.appendChild(img);
        if (item.drawingDescription) {
          const cap = document.createElement('div');
          cap.className = 'sa-drawing-caption';
          cap.textContent = item.drawingDescription;
          wrap.appendChild(cap);
        }
        frag.appendChild(wrap);
      } else if (item.drawingDescription) {
        const note = document.createElement('div');
        note.className = 'sa-drawing-note';
        note.innerHTML = '<svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2"/>'
          + '<circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>';
        const noteText = document.createElement('span');
        noteText.textContent = item.drawingDescription;
        note.appendChild(noteText);
        frag.appendChild(note);
      }
    }
    return frag;
  }

  /* ── Build editable block (appended to containerEl) ─────────────────────── */
  /**
   * Appends read-content + edit-form to containerEl.
   * Returns references needed by _attachBlockController.
   */
  function _buildEditableBlock(containerEl, item, mcqLookup, isMcq, mcqKey) {
    // ── Read content ──
    const readContent = document.createElement('div');
    readContent.className = 'sa-read-content';
    readContent.appendChild(_buildAnswerContentEl(item, mcqLookup));
    containerEl.appendChild(readContent);

    // ── Edit form ──
    const editForm = document.createElement('div');
    editForm.className = 'sa-edit-form';

    let textarea    = null;
    let mcqEditWrap = null;

    if (isMcq) {
      // ── MCQ edit: radio rows with editable text inputs ──
      const hint = document.createElement('div');
      hint.className = 'sa-mcq-edit-hint';
      hint.textContent = 'Select the correct option and edit any option text if extraction was incorrect. Option text changes are session-only; selection changes are saved permanently.';
      editForm.appendChild(hint);

      mcqEditWrap = document.createElement('div');
      mcqEditWrap.className = 'sa-mcq-edit-wrap';

      const editOpts   = _getMcqEditOptions(item, mcqLookup, mcqKey);
      const radioName  = 'mcq_opt_' + item.id;
      const currentSel = (item.studentSelectedOption || '').toLowerCase();

      editOpts.forEach(({ letter, text }) => {
        const row = document.createElement('div');
        row.className = 'sa-mcq-edit-row';

        const radio = document.createElement('input');
        radio.type    = 'radio';
        radio.name    = radioName;
        radio.value   = letter;
        radio.className = 'sa-mcq-edit-radio';
        if (letter === currentSel) radio.checked = true;

        const letterSpan = document.createElement('span');
        letterSpan.className = 'sa-mcq-edit-letter';
        letterSpan.textContent = letter.toUpperCase() + '.';

        const textInput = document.createElement('input');
        textInput.type      = 'text';
        textInput.className = 'sa-mcq-edit-text-input';
        textInput.value     = text;
        textInput.placeholder = 'Option text…';

        row.appendChild(radio);
        row.appendChild(letterSpan);
        row.appendChild(textInput);
        mcqEditWrap.appendChild(row);
      });

      editForm.appendChild(mcqEditWrap);
    } else {
      // ── Open-ended edit: plain textarea ──
      textarea = document.createElement('textarea');
      textarea.className = 'sa-edit-textarea';
      textarea.rows = 4;
      editForm.appendChild(textarea);
    }

    // ── Shared action row ──
    const editActions = document.createElement('div');
    editActions.className = 'sa-edit-actions';

    const saveBtn = document.createElement('button');
    saveBtn.className = 'sa-btn sa-btn-save';
    saveBtn.innerHTML = _svgSave() + 'Save';
    editActions.appendChild(saveBtn);

    const cancelBtn = document.createElement('button');
    cancelBtn.className = 'sa-btn sa-btn-cancel';
    cancelBtn.innerHTML = _svgCancel() + 'Cancel';
    editActions.appendChild(cancelBtn);

    const savingIndicator = document.createElement('span');
    savingIndicator.className = 'sa-edit-saving-indicator';
    savingIndicator.textContent = 'Saving…';
    editActions.appendChild(savingIndicator);

    const saveErrorEl = document.createElement('span');
    saveErrorEl.className = 'sa-edit-error';
    editActions.appendChild(saveErrorEl);

    editForm.appendChild(editActions);
    containerEl.appendChild(editForm);

    return { readContent, editForm, textarea, mcqEditWrap, saveBtn, cancelBtn, savingIndicator, saveErrorEl };
  }

  /* ── Block controller: wires up Edit / Save / Cancel / Delete ────────────── */
  function _attachBlockController(opts) {
    const {
      blockEl, item, isMcq, mcqLookup, mcqKey,
      editBtn, deleteBtn,
      editForm, textarea, mcqEditWrap,
      saveBtn, cancelBtn,
      savingIndicator, saveErrorEl,
      readContent, modifiedBadge,
      parentCard, cardsRoot,
    } = opts;

    // ── Edit ──
    editBtn.addEventListener('click', () => {
      readContent.style.display = 'none';

      if (isMcq) {
        // Refresh radio + text inputs from current item state + session overrides
        const radioName  = 'mcq_opt_' + item.id;
        const currentSel = (item.studentSelectedOption || '').toLowerCase();
        const freshOpts  = _getMcqEditOptions(item, mcqLookup, mcqKey);
        const radios     = [...mcqEditWrap.querySelectorAll(`input[name="${radioName}"]`)];
        const textInputs = [...mcqEditWrap.querySelectorAll('.sa-mcq-edit-text-input')];

        radios.forEach((radio, i) => {
          radio.checked = radio.value === currentSel;
          if (textInputs[i] && freshOpts[i]) textInputs[i].value = freshOpts[i].text;
        });
      } else {
        textarea.value = (item.studentAnswer || '').trim();
        textarea.focus();
      }

      saveErrorEl.style.display = 'none';
      editForm.classList.add('sa-edit-open');
      editBtn.style.display = 'none';
    });

    // ── Cancel ──
    cancelBtn.addEventListener('click', () => {
      editForm.classList.remove('sa-edit-open');
      readContent.style.display = '';
      editBtn.style.display     = '';
      saveErrorEl.style.display = 'none';
    });

    // ── Save ──
    saveBtn.addEventListener('click', async () => {
      saveBtn.disabled              = true;
      savingIndicator.style.display = 'inline';
      saveErrorEl.style.display     = 'none';

      try {
        if (isMcq) {
          // Collect radio selection + text input values
          const radioName  = 'mcq_opt_' + item.id;
          const radios     = [...mcqEditWrap.querySelectorAll(`input[name="${radioName}"]`)];
          const textInputs = [...mcqEditWrap.querySelectorAll('.sa-mcq-edit-text-input')];

          const selectedRadio = radios.find(r => r.checked);
          const newSel = selectedRadio
            ? selectedRadio.value.toLowerCase()
            : (item.studentSelectedOption || '').toLowerCase();

          // Build marks: selected → SELECTED, others → CROSSED_OUT
          const newMarks = {};
          radios.forEach(r => {
            newMarks[r.value.toLowerCase()] = r.value.toLowerCase() === newSel ? 'SELECTED' : 'CROSSED_OUT';
          });
          const newMarksStr = JSON.stringify(newMarks);

          // Collect option text into session override
          const newOptText = {};
          radios.forEach((r, i) => {
            if (textInputs[i]) newOptText[r.value.toLowerCase()] = textInputs[i].value;
          });

          // PATCH selection + marks to DB
          await apiFetch(`/extraction/answers/${item.id}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ studentSelectedOption: newSel, mcqOptionMarks: newMarksStr }),
          });

          // Update item in memory
          item.studentSelectedOption = newSel;
          item.mcqOptionMarks        = newMarksStr;

          // Store option text session overrides
          _mcqOptTextOverrides[mcqKey] = newOptText;

          // Rebuild read content to reflect new state
          readContent.innerHTML = '';
          readContent.appendChild(_buildAnswerContentEl(item, mcqLookup));

        } else {
          // Open-ended: PATCH student_answer
          const newText = textarea.value;
          await apiFetch(`/extraction/answers/${item.id}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ studentAnswer: newText }),
          });

          // Update item + read view
          item.studentAnswer = newText;
          const ansEl = readContent.querySelector('.sa-answer-text');
          if (ansEl) {
            const trimmed = newText.trim();
            ansEl.classList.toggle('sa-blank', !trimmed);
            ansEl.textContent = trimmed || '(blank)';
          }
        }

        // Mark modified
        _modifiedItems.add(item.id);
        modifiedBadge.style.display = 'inline-flex';
        blockEl.classList.add('sa-block--modified');

        // Close edit form
        editForm.classList.remove('sa-edit-open');
        readContent.style.display = '';
        editBtn.style.display     = '';

      } catch (err) {
        saveErrorEl.textContent   = 'Save failed: ' + (err.message || 'Unknown error');
        saveErrorEl.style.display = 'inline';
      } finally {
        saveBtn.disabled              = false;
        savingIndicator.style.display = 'none';
      }
    });

    // ── Delete ──
    deleteBtn.addEventListener('click', async () => {
      const confirmed = window.confirm(
        'This will permanently delete this question and its extracted answer.\n\nThis action cannot be undone.'
      );
      if (!confirmed) return;

      deleteBtn.disabled = true;
      try {
        await apiFetch(`/extraction/answers/${item.id}`, { method: 'DELETE' });

        if (parentCard) {
          // Sub-block: remove sub only; if last sub, remove the whole card
          const remainingSubs = parentCard.querySelectorAll('.sa-sub-block');
          if (remainingSubs.length <= 1) {
            parentCard.remove();
          } else {
            blockEl.remove();
          }
        } else {
          blockEl.remove();
        }

        // If no cards remain, show empty state
        if (cardsRoot && !cardsRoot.querySelector('.sa-card')) {
          cardsRoot.innerHTML = '<div class="sa-no-answers">All answer blocks have been deleted.</div>';
        }
      } catch (err) {
        window.alert('Delete failed: ' + (err.message || 'Unknown error'));
        deleteBtn.disabled = false;
      }
    });
  }

  /* ── Main DOM-building renderer ──────────────────────────────────────────── */
  function _renderCards(items, qTextLookup, mcqLookup, containerEl) {
    if (!items.length) {
      containerEl.innerHTML = '<div class="sa-no-answers">No answers were extracted for this student.</div>';
      return;
    }

    const groups    = _groupAnswerItems(items);
    const cardsWrap = document.createElement('div');
    cardsWrap.className = 'sa-cards';

    for (const group of groups) {
      const qLabel    = group.parent;
      const qText     = qTextLookup[qLabel] || '';
      const isGrouped = group.items.length > 1 && group.items.every(it => it._sub);

      const card = document.createElement('div');
      card.className = 'sa-card';

      // Top row: label + modified badge
      const topRow = document.createElement('div');
      topRow.className = 'sa-block-top-row';

      const qLabelEl = document.createElement('div');
      qLabelEl.className = 'sa-q-label';
      qLabelEl.textContent = qLabel;
      topRow.appendChild(qLabelEl);

      const modifiedBadge = document.createElement('span');
      modifiedBadge.className = 'sa-modified-badge';
      modifiedBadge.style.display = 'none';
      modifiedBadge.textContent = 'Modified';
      topRow.appendChild(modifiedBadge);

      card.appendChild(topRow);

      if (qText) {
        const qTextEl = document.createElement('div');
        qTextEl.className = 'sa-q-text';
        qTextEl.textContent = qText;
        card.appendChild(qTextEl);
      }

      if (isGrouped) {
        // ── Grouped: per-sub-block controls ──
        for (const item of group.items) {
          const subKey  = qLabel + item._sub;
          const subText = qTextLookup[subKey] || '';
          const isMcq   = ['multiple_choice','true_false'].includes((item.answerType||'').toLowerCase());
          const mcqKey  = qLabel + item._sub;

          const subBlock = document.createElement('div');
          subBlock.className = 'sa-sub-block';

          const subTopRow = document.createElement('div');
          subTopRow.className = 'sa-block-top-row';

          const subLabel = document.createElement('div');
          subLabel.className = 'sa-sub-label';
          subLabel.textContent = 'Part ' + item._sub.toUpperCase();
          subTopRow.appendChild(subLabel);

          const subModBadge = document.createElement('span');
          subModBadge.className = 'sa-modified-badge';
          subModBadge.style.display = _modifiedItems.has(item.id) ? 'inline-flex' : 'none';
          subModBadge.textContent = 'Modified';
          subTopRow.appendChild(subModBadge);

          const subControls = document.createElement('div');
          subControls.className = 'sa-block-controls';

          const subEditBtn = document.createElement('button');
          subEditBtn.className = 'sa-btn sa-btn-edit';
          subEditBtn.innerHTML = _svgEdit() + 'Edit';
          subEditBtn.title = 'Edit this answer';
          subControls.appendChild(subEditBtn);

          const subDeleteBtn = document.createElement('button');
          subDeleteBtn.className = 'sa-btn sa-btn-delete';
          subDeleteBtn.innerHTML = _svgTrash() + 'Delete';
          subDeleteBtn.title = 'Delete this answer block';
          subControls.appendChild(subDeleteBtn);

          subTopRow.appendChild(subControls);
          subBlock.appendChild(subTopRow);

          if (subText) {
            const subTextEl = document.createElement('div');
            subTextEl.className = 'sa-sub-q-text';
            subTextEl.textContent = subText;
            subBlock.appendChild(subTextEl);
          }

          if (_modifiedItems.has(item.id)) subBlock.classList.add('sa-block--modified');

          const { readContent, editForm, textarea, mcqEditWrap, saveBtn, cancelBtn, savingIndicator, saveErrorEl }
            = _buildEditableBlock(subBlock, item, mcqLookup, isMcq, mcqKey);

          _attachBlockController({
            blockEl: subBlock, item, isMcq, mcqLookup, mcqKey,
            editBtn: subEditBtn, deleteBtn: subDeleteBtn,
            editForm, textarea, mcqEditWrap,
            saveBtn, cancelBtn, savingIndicator, saveErrorEl,
            readContent, modifiedBadge: subModBadge,
            parentCard: card, cardsRoot: cardsWrap,
          });

          card.appendChild(subBlock);
        }
      } else {
        // ── Simple card: controls on card top row ──
        const item  = group.items[0];
        const isMcq = ['multiple_choice','true_false'].includes((item.answerType||'').toLowerCase());
        const mcqKey = qLabel + (item._sub || '');

        const controls = document.createElement('div');
        controls.className = 'sa-block-controls';

        const editBtn = document.createElement('button');
        editBtn.className = 'sa-btn sa-btn-edit';
        editBtn.innerHTML = _svgEdit() + 'Edit';
        editBtn.title = 'Edit this answer';
        controls.appendChild(editBtn);

        const deleteBtn = document.createElement('button');
        deleteBtn.className = 'sa-btn sa-btn-delete';
        deleteBtn.innerHTML = _svgTrash() + 'Delete';
        deleteBtn.title = 'Delete this answer block';
        controls.appendChild(deleteBtn);

        topRow.appendChild(controls);

        if (_modifiedItems.has(item.id)) {
          modifiedBadge.style.display = 'inline-flex';
          card.classList.add('sa-block--modified');
        }

        const { readContent, editForm, textarea, mcqEditWrap, saveBtn, cancelBtn, savingIndicator, saveErrorEl }
          = _buildEditableBlock(card, item, mcqLookup, isMcq, mcqKey);

        _attachBlockController({
          blockEl: card, item, isMcq, mcqLookup, mcqKey,
          editBtn, deleteBtn,
          editForm, textarea, mcqEditWrap,
          saveBtn, cancelBtn, savingIndicator, saveErrorEl,
          readContent, modifiedBadge,
          parentCard: null, cardsRoot: cardsWrap,
        });
      }

      cardsWrap.appendChild(card);
    }

    containerEl.appendChild(cardsWrap);
  }

  /* ── Render ──────────────────────────────────────────────────────────────── */
  const qlu        = _buildQTextLookup(modelAnswers);
  const mcqLu      = _buildMcqLookup(modelAnswers);
  const answerBody = document.getElementById('pvAnswerBody');
  answerBody.innerHTML = '';
  _renderCards(answers, qlu, mcqLu, answerBody);

  /* ════════════════════════════════════════════════════════════════════════
     Student Answer Export — CSV / Excel (.xlsx) / PDF
     All helpers share the IIFE-scoped variables:
       answers, exam, extraction, qlu, mcqLu,
       _mcqOptTextOverrides, _modifiedItems, _parseQId
  ════════════════════════════════════════════════════════════════════════ */

  // Lazy-load SheetJS from CDN only when user requests Excel export.
  async function _saLoadSheetJS() {
    if (window.XLSX) return;
    await new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src    = 'https://cdnjs.cloudflare.com/ajax/libs/xlsx/0.18.5/xlsx.full.min.js';
      s.onload = resolve;
      s.onerror = () => reject(new Error('Failed to load Excel library. Please check your internet connection.'));
      document.head.appendChild(s);
    });
  }

  const _SA_ANS_TYPE_LABEL = {
    recall_general:         'Open-ended (Recall)',
    recall_differentiation: 'Open-ended (Differentiation)',
    mathematical:           'Mathematical',
    diagrammatic_aoa:       'Diagrammatic (AOA)',
    multiple_choice:        'MCQ',
    multiple_choice_single: 'MCQ (Single select)',
    true_false:             'True / False',
  };

  const _SA_EXPORT_COLS = [
    'QuestionID','SubQuestion','QuestionText','AnswerType',
    'StudentAnswer','SelectedOption','MCQOptions',
    'HasDrawing','DrawingNote','Modified',
  ];

  /**
   * Flatten all answer items into export rows.
   * Grouped questions (Q1a / Q1b) each produce their own row.
   * MCQ options are sourced from mcqLu + session text overrides (_mcqOptTextOverrides).
   */
  function _buildStudentExportRows() {
    const ALPHA = 'ABCDEFGHIJ';
    const rows  = [];
    for (const item of answers) {
      const { parent, sub } = _parseQId(item.questionId || '');
      const mcqKey  = parent + (sub || '');
      const qText   = qlu[mcqKey] || qlu[parent] || '';
      const atype   = (item.answerType || '').toLowerCase();
      const isMcq   = atype === 'multiple_choice' || atype === 'true_false';

      let selectedOption = '';
      let optionsStr     = '';

      if (isMcq) {
        const modelOpts   = (mcqLu[mcqKey] || mcqLu[parent] || null);
        const sessionTx   = _mcqOptTextOverrides[mcqKey] || {};

        // Build options string with session overrides applied
        if (modelOpts && modelOpts.length) {
          optionsStr = modelOpts.map((opt, i) => {
            const letter = ALPHA[i] || '';
            const text   = sessionTx[letter.toLowerCase()] !== undefined
              ? sessionTx[letter.toLowerCase()]
              : (opt.text || '');
            return letter + '. ' + text;
          }).join(' | ');
        }

        // Determine selected option from mcqOptionMarks first, then fallback
        let marks = {};
        try { if (item.mcqOptionMarks) marks = JSON.parse(item.mcqOptionMarks); } catch (_) {}
        const selEntry  = Object.entries(marks).find(([, st]) => st === 'SELECTED');
        const selLetter = (selEntry ? selEntry[0] : (item.studentSelectedOption || '')).toUpperCase();
        if (selLetter) {
          const selIdx     = selLetter.charCodeAt(0) - 65;
          const selOptText = modelOpts && modelOpts[selIdx]
            ? (sessionTx[selLetter.toLowerCase()] !== undefined
                ? sessionTx[selLetter.toLowerCase()]
                : (modelOpts[selIdx].text || ''))
            : (sessionTx[selLetter.toLowerCase()] || '');
          selectedOption = selLetter + (selOptText ? '. ' + selOptText : '');
        }
      }

      rows.push({
        QuestionID:     parent,
        SubQuestion:    sub ? sub.toUpperCase() : '',
        QuestionText:   qText,
        AnswerType:     _SA_ANS_TYPE_LABEL[atype] || item.answerType || '',
        StudentAnswer:  isMcq ? '' : (item.studentAnswer || ''),
        SelectedOption: selectedOption,
        MCQOptions:     optionsStr,
        HasDrawing:     item.hasDrawing ? 'Yes' : 'No',
        DrawingNote:    item.drawingDescription || '',
        Modified:       _modifiedItems.has(item.id) ? 'Yes' : 'No',
      });
    }
    return rows;
  }

  function _saExportCSV(rows, filename) {
    function _cell(v) {
      const s = String(v == null ? '' : v);
      return (s.includes(',') || s.includes('"') || s.includes('\n'))
        ? '"' + s.replace(/"/g, '""') + '"' : s;
    }
    const hdr  = _SA_EXPORT_COLS.join(',');
    const body = rows.map(r => _SA_EXPORT_COLS.map(c => _cell(r[c] ?? '')).join(',')).join('\n');
    const blob = new Blob(['\uFEFF' + hdr + '\n' + body], { type: 'text/csv;charset=utf-8;' });
    const url  = URL.createObjectURL(blob);
    const a    = Object.assign(document.createElement('a'), {
      href: url, download: filename.replace(/[/\\?%*:|"<>]/g, '_'),
    });
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }

  async function _saExportXLSX(rows, filename) {
    try { await _saLoadSheetJS(); }
    catch (e) { alert(e.message); return; }
    const { XLSX } = window;
    const aoa = [_SA_EXPORT_COLS, ...rows.map(r => _SA_EXPORT_COLS.map(c => r[c] ?? ''))];
    const ws  = XLSX.utils.aoa_to_sheet(aoa);
    _SA_EXPORT_COLS.forEach((_, ci) => {
      const addr = XLSX.utils.encode_cell({ r: 0, c: ci });
      if (ws[addr]) ws[addr].s = { font: { bold: true } };
    });
    ws['!cols'] = _SA_EXPORT_COLS.map(col => ({
      wch: Math.min(Math.max(col.length, ...rows.map(r => String(r[col] ?? '').length)) + 2, 60),
    }));
    const wb = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(wb, ws, 'Student Answers');
    XLSX.writeFile(wb, filename.replace(/[/\\?%*:|"<>]/g, '_'));
  }

  function _saExportPDF(rows, studentInfo, examInfo) {
    function _e(s) {
      return String(s ?? '')
        .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }

    // Group flat rows by parent QuestionID for hierarchical PDF layout
    const grouped = [];
    let cur = null;
    for (const row of rows) {
      if (!cur || cur.qId !== row.QuestionID) {
        cur = { qId: row.QuestionID, parts: [] };
        grouped.push(cur);
      }
      cur.parts.push(row);
    }

    function _partHtml(part, showSubLabel) {
      const subLabel  = showSubLabel && part.SubQuestion
        ? `<div class="sub-lbl">Part ${_e(part.SubQuestion)}</div>` : '';
      const qtext     = part.QuestionText
        ? `<div class="f-qtxt">${_e(part.QuestionText)}</div>` : '';
      const typeChip  = part.AnswerType
        ? `<span class="f-chip f-type">${_e(part.AnswerType)}</span>` : '';
      const modChip   = part.Modified === 'Yes'
        ? `<span class="f-chip f-mod">Manually Edited</span>` : '';

      let ansBlock;
      if (part.SelectedOption) {
        ansBlock = `<div class="f-lbl">Selected Option</div><div class="f-ans">${_e(part.SelectedOption)}</div>`;
        if (part.MCQOptions) {
          ansBlock += `<div class="f-lbl">Options</div><div class="f-opts">`
            + part.MCQOptions.split(' | ').map(o => `<div class="f-opt">${_e(o)}</div>`).join('')
            + `</div>`;
        }
      } else {
        const ans = part.StudentAnswer || '';
        ansBlock = `<div class="f-lbl">Student Answer</div>`
          + `<div class="f-ans">${ans ? _e(ans) : '<em style="color:#9ca3af">—</em>'}</div>`;
      }

      const drawBlock = part.HasDrawing === 'Yes'
        ? `<div class="f-drawing">[Drawing attached]${part.DrawingNote ? ' — ' + _e(part.DrawingNote) : ''}</div>`
        : '';

      return subLabel + qtext
        + `<div class="f-meta">${typeChip}${modChip}</div>`
        + ansBlock + drawBlock;
    }

    const questionsHtml = grouped.map(grp => {
      const hasSubs = grp.parts.length > 1 || !!grp.parts[0]?.SubQuestion;
      const body = hasSubs
        ? grp.parts.map(p => `<div class="sub-block">${_partHtml(p, true)}</div>`).join('')
        : _partHtml(grp.parts[0], false);
      return `<div class="q-card"><div class="q-id">${_e(grp.qId)}</div>${body}</div>`;
    }).join('');

    const html = `<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8">
<title>Student Answers — ${_e(studentInfo.name || 'Unknown')}</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',Arial,sans-serif;font-size:11pt;color:#1e293b;background:#fff;padding:32px 44px 56px}
.hd{border-bottom:2.5px solid #28405b;padding-bottom:16px;margin-bottom:24px}
.hd-title{font-size:22pt;font-weight:800;color:#28405b}
.hd-sub{font-size:13pt;font-weight:600;color:#446b95;margin-top:5px}
.hd-meta{font-size:10.5pt;color:#6b7280;margin-top:10px;display:flex;gap:18px;flex-wrap:wrap}
.q-card{border:1px solid #d1d5db;border-radius:8px;padding:18px 22px;margin-bottom:18px;page-break-inside:avoid;break-inside:avoid}
.q-id{font-size:14pt;font-weight:800;color:#446b95;margin-bottom:9px}
.f-qtxt{font-size:11pt;color:#1e293b;margin-bottom:10px;line-height:1.55}
.f-meta{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:10px}
.f-chip{display:inline-block;padding:2px 10px;border-radius:999px;font-size:9pt;font-weight:700;background:#f1f5f9;color:#475569;border:1px solid #e2e8f0}
.f-chip.f-type{background:#eff6ff;color:#1d4ed8;border-color:#bfdbfe}
.f-chip.f-mod{background:#fffbeb;color:#92400e;border-color:#fde68a}
.f-lbl{font-size:9pt;font-weight:700;color:#6b7280;text-transform:uppercase;letter-spacing:.05em;margin:10px 0 4px}
.f-ans{font-size:11pt;background:#f8fafc;border:1px solid #e2e8f0;border-radius:6px;padding:8px 12px;line-height:1.5;white-space:pre-wrap}
.f-opts{display:flex;flex-direction:column;gap:3px}
.f-opt{font-size:10.5pt;color:#374151;padding:3px 0}
.f-drawing{font-size:10pt;color:#6b7280;font-style:italic;margin-top:8px;padding:6px 10px;background:#f8fafc;border-radius:6px;border:1px solid #e2e8f0}
.sub-block{border-top:1px solid #e5e7eb;padding-top:13px;margin-top:13px}
.sub-lbl{font-size:11pt;font-weight:700;color:#6895bc;margin-bottom:7px}
.footer{margin-top:32px;border-top:1px solid #e5e7eb;padding-top:12px;font-size:9pt;color:#9ca3af;text-align:center}
@media print{body{padding:16px 24px}.q-card{page-break-inside:avoid;break-inside:avoid}}
</style></head><body>
<div class="hd">
  <div class="hd-title">${_e(examInfo.name || 'Exam')}</div>
  <div class="hd-sub">Student Answer Sheet</div>
  <div class="hd-meta">
    ${studentInfo.name    ? `<span>&#128100; ${_e(studentInfo.name)}</span>` : ''}
    ${studentInfo.idNum   ? `<span>ID: ${_e(studentInfo.idNum)}</span>` : ''}
    ${studentInfo.section ? `<span>Section ${_e(studentInfo.section)}</span>` : ''}
    ${examInfo.date       ? `<span>&#128197; ${_e(examInfo.date)}</span>` : ''}
    ${examInfo.type       ? `<span>&#128203; ${_e(examInfo.type)}</span>` : ''}
    ${examInfo.totalMarks ? `<span>Total: ${_e(String(examInfo.totalMarks))} marks</span>` : ''}
    <span>${answers.length} answer${answers.length !== 1 ? 's' : ''}</span>
  </div>
</div>
${questionsHtml}
<div class="footer">Exported from Taqdeer &middot; ${_e(new Date().toLocaleString())}</div>
</body></html>`;

    const w = window.open('', '_blank', 'width=820,height=920,scrollbars=yes');
    if (!w) { alert('Pop-up blocked. Please allow pop-ups and try again.'); return; }
    w.document.write(html);
    w.document.close();
    w.focus();
    setTimeout(() => w.print(), 650);
  }

  /** Main student-export entry point — dispatches to CSV / XLSX / PDF. */
  async function exportStudentAnswers(format) {
    document.getElementById('pvExportWrap')?.classList.remove('open');
    const rows = _buildStudentExportRows();
    if (!rows.length) { alert('No answers to export.'); return; }

    const typeMap = { midterm:'Midterm', final:'Final', quiz:'Quiz', assignment:'Assignment' };
    const studentInfo = {
      name:    extraction.studentName    || null,
      idNum:   extraction.studentIdNum   || null,
      section: extraction.studentSection || null,
    };
    const examInfo = {
      name:       exam.name  || '',
      type:       typeMap[exam.type] || exam.type || '',
      date:       exam.date
        ? new Date(exam.date).toLocaleDateString(undefined, { year:'numeric', month:'long', day:'numeric' })
        : '',
      totalMarks: exam.totalMarks || null,
    };

    const safeName     = (studentInfo.name || 'Student').replace(/[/\\?%*:|"<>]/g, '_');
    const baseFilename = `${exam.name || 'Exam'} - ${safeName} - Student Answers`;

    if (format === 'csv') {
      _saExportCSV(rows, baseFilename + '.csv');
    } else if (format === 'xlsx') {
      await _saExportXLSX(rows, baseFilename + '.xlsx');
    } else if (format === 'pdf') {
      _saExportPDF(rows, studentInfo, examInfo);
    }
  }

  /* ── Show export button and wire dropdown events ──────────────────────────── */
  document.getElementById('pvExportWrap').style.display = 'inline-flex';

  document.getElementById('pvExportTrigger').addEventListener('click', e => {
    e.stopPropagation();
    document.getElementById('pvExportWrap').classList.toggle('open');
  });

  document.addEventListener('click', () => {
    document.getElementById('pvExportWrap')?.classList.remove('open');
  });

  document.getElementById('pvExportDropdown').addEventListener('click', e => {
    const btn = e.target.closest('[data-pvexport]');
    if (btn) exportStudentAnswers(btn.dataset.pvexport);
  });

})();
