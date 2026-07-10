// =============================================================================
// js/auth.js
// -----------------------------------------------------------------------------
// Everything about "who is logged in" and "are they allowed to be on this
// page": JWT decoding, the session object, login/logout, the per-page role
// guard, and the idle/expiry watchdog. `js/api.js` imports `getSession` and
// `logout` from here so it knows how to attach/clear the session; this file
// imports `API_URL` from `js/api.js` only inside `login()`, where it's
// actually needed at call time (not at module-load time), so the circular
// import between the two files resolves fine under ES modules.
// =============================================================================

import { API_URL } from './api.js';

// ---------------------------------------------------------------------------
// SESSION HELPERS
// ---------------------------------------------------------------------------
// We store the real JWT the backend issues at login under localStorage key
// 'token'. The JWT payload (name/email/role/department) is NOT secret info a
// user shouldn't see -- it's their own identity -- so we decode it client
// side purely to decide which UI to show. The backend independently verifies
// the token's cryptographic signature on every request, so a user editing
// their own localStorage cannot actually grant themselves extra permissions.

// Simple Base64URL decoder to read JWT payload without external libraries
export function parseJwt(token) {
  try {
    const base64Url = token.split('.')[1];
    const base64 = base64Url.replace(/-/g, '+').replace(/_/g, '/');
    const jsonPayload = decodeURIComponent(atob(base64).split('').map(function (c) {
      return '%' + ('00' + c.charCodeAt(0).toString(16)).slice(-2);
    }).join(''));
    return JSON.parse(jsonPayload);
  } catch (e) {
    return null;
  }
}

export function getSession() {
  const token = localStorage.getItem('token');
  if (!token) return null;
  const payload = parseJwt(token);
  if (!payload) return null;
  // exp is in seconds since epoch; Date.now() is in milliseconds.
  if (payload.exp && payload.exp * 1000 < Date.now()) {
    localStorage.removeItem('token');
    return null;
  }
  return { token, ...payload };
}

export function logout() {
  localStorage.removeItem('token');
  window.location.href = 'index.html';
}

// Performs the actual POST /auth/login call and stores the resulting token.
// Kept here (rather than in api.js) since it's session/identity logic, not
// a generic authenticated request -- there's no session yet when this runs.
//
// `identifier` may be EITHER an email address OR a username (Data Quality &
// Usability requirement #6) -- the backend tries both, see
// backend/services/auth_service.py -> login().
export async function login(identifier, password) {
  const response = await fetch(`${API_URL}/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ identifier, password }),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || 'Invalid email/username or password');
  localStorage.setItem('token', data.token);
  return data;
}

// -----------------------------------------------------------------------------
// ROUTE GUARD (runs on every page)
// -----------------------------------------------------------------------------
// Each page declares which roles may view it. "self" pages (staff/customer)
// just require *some* valid session -- everyone is allowed to see their own
// custody ledger, so there's no extra role check needed beyond being logged in.
//
// NOTE: this is the SECOND line of defense. The FIRST line of defense is the
// small synchronous <script> block placed at the very top of <head> in each
// dashboard HTML file (before ANY page content is parsed) -- see the comment
// at the top of admin.html/manager.html/staff.html/customer.html for why
// that matters. This function re-checks the same rules once the full page
// (including these modules) has loaded, as a safety net.
const PAGE_ACCESS_RULES = {
  'admin.html': ['super_admin', 'admin'],
  'manager.html': ['manager', 'super_admin', 'admin'],
  'staff.html': ['staff', 'super_admin', 'admin'],
  'customer.html': ['customer', 'super_admin', 'admin'],
};

export function currentPageName() {
  const parts = window.location.pathname.split('/');
  return parts[parts.length - 1] || 'index.html';
}

export function checkAccess() {
  const session = getSession();
  const page = currentPageName();
  const onLoginPage = page === 'index.html' || page === '';

  if (onLoginPage) {
    if (session) redirectByUserRole(session.role);
    return;
  }

  const allowedRoles = PAGE_ACCESS_RULES[page];
  if (allowedRoles) {
    if (!session) { window.location.href = 'index.html'; return; }
    if (!allowedRoles.includes(session.role)) {
      alert('Access Denied: You do not have permission to view this page.');
      logout();
    }
  }
}

export function redirectByUserRole(role) {
  if (role === 'super_admin' || role === 'admin') {
    window.location.href = 'admin.html';
  } else if (role === 'manager') {
    window.location.href = 'manager.html';
  } else if (role === 'staff') {
    window.location.href = 'staff.html';
  } else if (role === 'customer') {
    window.location.href = 'customer.html';
  } else {
    alert('No dashboard is configured for this account role yet.');
    logout();
  }
}

// -----------------------------------------------------------------------------
// IDLE / EXPIRY WATCHDOG
// -----------------------------------------------------------------------------
// A user might leave a dashboard tab open for a long time without clicking
// anything (so no apiRequest() ever runs to trigger the 401-based logout in
// api.js above). This timer polls the token's expiration every 15 seconds
// and forces an immediate redirect to the login page the moment it expires,
// so an abandoned/unlocked tab doesn't stay "logged in" looking indefinitely.
const IDLE_CHECK_INTERVAL_MS = 15000;

export function startIdleWatchdog() {
  setInterval(() => {
    const page = currentPageName();
    const onLoginPage = page === 'index.html' || page === '';
    if (onLoginPage) return; // nothing to expire on the login screen itself

    const token = localStorage.getItem('token');
    if (!token) return; // already logged out, nothing to watch

    const payload = parseJwt(token);
    const expired = !payload || !payload.exp || payload.exp * 1000 < Date.now();
    if (expired) {
      localStorage.removeItem('token');
      window.location.href = 'index.html';
    }
  }, IDLE_CHECK_INTERVAL_MS);
}
