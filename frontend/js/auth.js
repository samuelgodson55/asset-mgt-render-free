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

import { API_URL, buildErrorMessage } from './api.js';

// ---------------------------------------------------------------------------
// SESSION HELPERS
// ---------------------------------------------------------------------------
// We keep a lightweight, non-secret session object in localStorage after
// login so the UI can decide which dashboard to show without ever needing
// to read the actual JWT from JavaScript. The real auth state now lives in
// the HttpOnly cookie the backend sets on login.

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

function clearSessionStorage() {
  localStorage.removeItem('session');
  localStorage.removeItem('token');
}

function persistSession(sessionData) {
  localStorage.setItem('session', JSON.stringify(sessionData));
}

export function getSession() {
  const sessionData = localStorage.getItem('session');
  const legacyToken = localStorage.getItem('token');

  if (!sessionData && !legacyToken) return null;

  if (sessionData) {
    try {
      const session = JSON.parse(sessionData);
      if (session.expires_at && session.expires_at * 1000 < Date.now()) {
        clearSessionStorage();
        return null;
      }
      return session;
    } catch (e) {
      clearSessionStorage();
      return null;
    }
  }

  const payload = parseJwt(legacyToken);
  if (!payload) {
    clearSessionStorage();
    return null;
  }
  if (payload.exp && payload.exp * 1000 < Date.now()) {
    clearSessionStorage();
    return null;
  }
  return { token: legacyToken, ...payload };
}

export async function logout() {
  clearSessionStorage();
  try {
    await fetch(`${API_URL}/auth/logout`, {
      method: 'POST',
      credentials: 'include',
    });
  } catch (e) {
    // Ignore logout failures; the page redirect below still completes.
  }
  window.location.href = '/';
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
    credentials: 'include',
  });
  const data = await response.json();
  if (!response.ok) throw new Error(buildErrorMessage(response, data, 'Invalid email/username or password'));

  // SECURITY: an account that requires 2FA (currently just super_admin --
  // see backend/services/auth_service.py's login()) does NOT get a real
  // session from this call alone -- the backend hasn't set the auth
  // cookie yet either. `data` here is either `mfa_setup_required` (never
  // enrolled yet: carries a fresh secret + otpauth URI to enroll with) or
  // `mfa_required` (already enrolled: needs a live code). Callers
  // (js/main.js's login form handler) must check for these BEFORE
  // treating a successful response as "logged in", and complete the
  // matching flow via confirmMfaSetup()/verifyMfa() below.
  if (data.mfa_setup_required || data.mfa_required) {
    return data;
  }

  persistSession({
    user_id: data.user_id,
    name: data.name,
    username: data.username,
    role: data.role,
    department: data.department,
    expires_at: data.expires_at,
    needs_password_reset: data.needs_password_reset,
  });
  return data;
}

// Completes FIRST-time 2FA enrollment: `mfaSetupToken`/`code` come from the
// mfa_setup_required response above and the code the person typed in after
// scanning/typing the secret into their authenticator app. On success this
// is the call that actually grants the session (the backend sets the real
// auth cookie here, not in login() above) -- see api/auth_api.py.
export async function confirmMfaSetup(mfaSetupToken, code) {
  const response = await fetch(`${API_URL}/auth/mfa/setup/confirm`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mfa_setup_token: mfaSetupToken, code }),
    credentials: 'include',
  });
  const data = await response.json();
  if (!response.ok) throw new Error(buildErrorMessage(response, data, 'Incorrect code. Please try again.'));

  persistSession({
    user_id: data.user_id,
    name: data.name,
    username: data.username,
    role: data.role,
    department: data.department,
    expires_at: data.expires_at,
    needs_password_reset: data.needs_password_reset,
  });
  return data;
}

// Completes login for an ALREADY-enrolled account: `mfaPendingToken` comes
// from the mfa_required response above, `code` is the 6-digit code from
// the person's authenticator app. Same session-granting shape as
// confirmMfaSetup() above.
export async function verifyMfa(mfaPendingToken, code) {
  const response = await fetch(`${API_URL}/auth/mfa/verify`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mfa_pending_token: mfaPendingToken, code }),
    credentials: 'include',
  });
  const data = await response.json();
  if (!response.ok) throw new Error(buildErrorMessage(response, data, 'Incorrect code. Please try again.'));

  persistSession({
    user_id: data.user_id,
    name: data.name,
    username: data.username,
    role: data.role,
    department: data.department,
    expires_at: data.expires_at,
    needs_password_reset: data.needs_password_reset,
  });
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
// Keys are the CLEAN urls each dashboard is served at (see
// middleware/clean_urls.py / nginx/default.conf.template's rewrite rules)
// -- "/admin", not "admin.html".
const PAGE_ACCESS_RULES = {
  '/admin': ['super_admin', 'admin'],
  '/manager': ['manager', 'super_admin', 'admin'],
  '/staff': ['staff', 'super_admin', 'admin'],
  '/customer': ['customer', 'super_admin', 'admin'],
};

export function currentPageName() {
  const path = window.location.pathname;
  if (path === '' || path === '/') return '/';
  // Trailing slash is cosmetic ("/admin/" and "/admin" are the same page).
  return path.replace(/\/+$/, '');
}

export async function checkAccess() {
  const session = getSession();
  const page = currentPageName();
  const onLoginPage = page === '/';

  if (onLoginPage) {
    if (!session) return;
    // BUGFIX: this used to redirect straight into the dashboard on the
    // strength of the localStorage 'session' flag alone. That flag is
    // just a UI convenience written at login time -- it has no idea
    // whether the real HttpOnly auth cookie is still valid server-side
    // (expired, cleared, rejected by the browser due to a cookie
    // domain/SameSite mismatch after a deploy, backend restart, etc.).
    // Trusting it here caused a redirect LOOP: "/" sends the user to e.g.
    // "/admin" on the stale flag -> "/admin"'s auth-guard.js makes the
    // same /api/auth/me check we do below, finds no valid cookie, clears
    // the flag, and bounces back to "/" -> which (before this fix) still
    // saw a session on the very first render and sent them straight back
    // to "/admin". The two pages ping-ponged forever and the login form
    // never got a chance to run.
    //
    // Verifying against the server FIRST, here, breaks the loop: an
    // invalid cookie now just clears the stale flag and leaves the user
    // on the login form instead of bouncing them anywhere.
    try {
      const response = await fetch(`${API_URL}/auth/me`, { credentials: 'include' });
      if (response.ok) {
        redirectByUserRole(session.role);
      } else {
        clearSessionStorage();
      }
    } catch (e) {
      // Network error / API unreachable -- don't strand the user on a
      // half-authenticated state, but also don't wipe a possibly-good
      // session just because the request failed to even complete.
    }
    return;
  }

  const allowedRoles = PAGE_ACCESS_RULES[page];
  if (allowedRoles) {
    if (!session) { window.location.href = '/'; return; }
    if (!allowedRoles.includes(session.role)) {
      alert('Access Denied: You do not have permission to view this page.');
      logout();
    }
  }
}

export function redirectByUserRole(role) {
  if (role === 'super_admin' || role === 'admin') {
    window.location.href = '/admin';
  } else if (role === 'manager') {
    window.location.href = '/manager';
  } else if (role === 'staff') {
    window.location.href = '/staff';
  } else if (role === 'customer') {
    window.location.href = '/customer';
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
    const onLoginPage = page === '/';
    if (onLoginPage) return; // nothing to expire on the login screen itself

    const session = getSession();
    if (!session) return; // already logged out, nothing to watch

    const expired = session.expires_at && session.expires_at * 1000 < Date.now();
    if (expired) {
      clearSessionStorage();
      window.location.href = '/';
    }
  }, IDLE_CHECK_INTERVAL_MS);
}
