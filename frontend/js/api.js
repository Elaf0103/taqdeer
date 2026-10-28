/**
 * api.js — Shared auth + fetch utilities for Taqdeer.
 * Include on every authenticated page:
 *   <script src="../js/api.js"></script>
 *
 * Provides four globals:
 *   API_BASE    — backend root URL
 *   apiFetch()  — fetch wrapper that attaches the JWT Authorization header
 *   authGuard() — redirects to login.html if no token is stored
 *   adminGuard()— redirects to dashboard.html if the stored role is not admin
 *   doLogout()  — clears all auth state then redirects to login.html
 */

const API_BASE = window.location.hostname.includes('ngrok')
  ? 'https://YOUR-NGROK-BACKEND-URL.ngrok-free.app'
  : 'http://localhost:8000';
console.log('[API]', API_BASE);

/**
 * apiFetch(path, opts)
 *
 * Wraps fetch() and automatically attaches:
 *   Authorization: Bearer <taqdeer_token>
 *   Content-Type: application/json  (omitted for FormData bodies)
 *
 * Throws an Error with message = backend detail on non-2xx responses.
 */
async function apiFetch(path, opts = {}) {
  const token  = localStorage.getItem('taqdeer_token');
  const isForm = opts.body instanceof FormData;

  const headers = {
    ...(isForm ? {} : { 'Content-Type': 'application/json' }),
    ...(token  ? { 'Authorization': `Bearer ${token}` } : {}),
    ...(opts.headers || {}),
  };

  let res;
  try {
    res = await fetch(API_BASE + path, { ...opts, headers });
  } catch (_networkErr) {
    // fetch() itself threw — server unreachable or no network
    const err = new Error('Failed to connect to the server.');
    err.status = 0;
    throw err;
  }

  if (!res.ok) {
    // 401 — token missing, expired, or invalid.
    // Clear auth state and redirect to login so the user can re-authenticate.
    if (res.status === 401) {
      localStorage.removeItem('taqdeer_token');
      localStorage.removeItem('taqdeer_profile');
      sessionStorage.setItem('taqdeer_auth_msg', 'Session expired. Please log in again.');
      window.location.replace('./login.html');
      // Throw anyway so the caller's code stops cleanly while the navigation is pending.
      const err = new Error('Session expired. Please log in again.');
      err.status = 401;
      throw err;
    }

    const data = await res.json().catch(() => ({}));
    let msg;
    if (typeof data.detail === 'string') {
      msg = data.detail;
    } else if (Array.isArray(data.detail) && data.detail.length > 0) {
      // Pydantic v2 validation errors — extract human-readable messages
      msg = data.detail.map(e => e.msg || JSON.stringify(e)).join('; ');
    } else if (data.detail) {
      msg = JSON.stringify(data.detail);
    } else if (data.message) {
      msg = data.message;
    } else {
      msg = `HTTP ${res.status}`;
    }
    const err = new Error(msg);
    err.status = res.status;
    throw err;
  }

  // 204 No Content has no body
  if (res.status === 204) return null;
  return res.json();
}

/**
 * authGuard()
 *
 * Redirect to login.html immediately if no JWT token is stored.
 * Call at the very top of every authenticated page's <script> block,
 * before any DOM manipulation or API calls.
 */
function authGuard() {
  const token = localStorage.getItem('taqdeer_token');
  if (!token) {
    window.location.replace('./login.html');
    return;
  }
  // Proactively check the JWT exp claim so pages never even start rendering
  // with a token that is already expired — avoids a 401 on the first API call.
  try {
    // JWT payload is the second base64url segment
    const b64 = token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
    const payload = JSON.parse(atob(b64));
    if (payload.exp && payload.exp * 1000 < Date.now()) {
      localStorage.removeItem('taqdeer_token');
      localStorage.removeItem('taqdeer_profile');
      sessionStorage.setItem('taqdeer_auth_msg', 'Session expired. Please log in again.');
      window.location.replace('./login.html');
    }
  } catch (_) {
    // Malformed token — let the first API call fail with 401 and handle it there.
  }
}

/**
 * adminGuard()
 *
 * Redirect non-admin users to dashboard.html.
 * Call after authGuard() on admin-only pages.
 */
function adminGuard() {
  const p = JSON.parse(localStorage.getItem('taqdeer_profile') || 'null');
  if (!p || p.role !== 'admin') {
    window.location.replace('./dashboard.html');
  }
}

/**
 * getDisplayName(profile)
 *
 * Returns the instructor's formatted display name with professional title prefix.
 * e.g. profile = { professionalTitle: 'dr', fullName: 'Rana Alshaikh' }
 *      → "Dr. Rana Alshaikh"
 * Falls back to fullName alone if no title is set or title is "none".
 */
const _TITLE_LABELS = {
  dr:   'Dr.',
  prof: 'Prof.',
  mr:   'Mr.',
  ms:   'Ms.',
  mrs:  'Mrs.',
  miss: 'Miss.',
  eng:  'Eng.',
  none: '',
};
function getDisplayName(profile) {
  if (!profile) return '';
  const t      = profile.professionalTitle;
  const prefix = (t && t !== 'none' && _TITLE_LABELS[t]) ? _TITLE_LABELS[t] + ' ' : '';
  return prefix + (profile.fullName || '');
}

/**
 * doLogout()
 *
 * Clear all auth-related localStorage keys, show a brief success message,
 * then redirect to the public landing page (index.html).
 * Call from any page inside frontend/pages/.
 */
function doLogout() {
  // 1. Clear auth credentials
  localStorage.removeItem('taqdeer_token');
  localStorage.removeItem('taqdeer_profile');
  // 2. Clear session/navigation state that may contain student or exam data
  localStorage.removeItem('currentExamId');
  localStorage.removeItem('exam-review-context');

  // 3. Notify backend (stateless — fire-and-forget, never blocks the redirect)
  try { fetch(API_BASE + '/auth/logout', { method: 'POST' }).catch(() => {}); } catch (_) {}

  // 4. Show a brief success overlay, then replace history entry so back-button
  //    cannot return to a protected page with stale UI.
  const overlay = document.createElement('div');
  overlay.style.cssText = [
    'position:fixed', 'inset:0', 'z-index:99999',
    'display:grid', 'place-items:center',
    'background:rgba(6,18,40,0.52)',
    'backdrop-filter:blur(14px)',
  ].join(';');
  overlay.innerHTML = `
    <div style="background:rgba(255,255,255,0.93);border-radius:20px;padding:40px 52px;
                text-align:center;box-shadow:0 28px 72px rgba(0,0,0,0.18);max-width:360px;width:90%;">
      <div style="width:52px;height:52px;border-radius:14px;background:rgba(16,185,129,0.12);
                  display:grid;place-items:center;margin:0 auto 18px;">
        <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="#10b981"
             stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
          <polyline points="20 6 9 17 4 12"/>
        </svg>
      </div>
      <h3 style="margin:0 0 8px;font-size:17px;font-weight:700;color:#065f46;font-family:Poppins,sans-serif;">
        Logged out successfully
      </h3>
      <p style="margin:0;font-size:13px;color:#6b7280;font-family:Poppins,sans-serif;">
        Redirecting to home…
      </p>
    </div>`;
  document.body.appendChild(overlay);

  setTimeout(() => {
    window.location.replace('../index.html');
  }, 1500);
}
