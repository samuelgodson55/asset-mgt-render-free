// =============================================================================
// js/auth-guard.js
// -----------------------------------------------------------------------------
// Synchronous, render-blocking page guard shared by every role-restricted
// dashboard (admin/manager/staff/customer.html). Loaded as a plain classic
// script tag -- deliberately NOT type="module" and NOT deferred/async --
// so it stays render-blocking and runs to completion BEFORE the rest of
// the page's markup paints. A deferred or module script would let an
// unauthorized visitor see a flash of the real dashboard before being
// redirected. Pairs with css/auth-guard.css, which keeps <body> hidden by
// default until this script adds the "authorized" class to <html>.
//
// USAGE (in each dashboard HTML file's <head>, in this order):
//     <link rel="stylesheet" href="css/auth-guard.css">
//     <script src="js/auth-guard.js" data-allowed-roles="super_admin,admin"></script>
//
// Previously this whole block (JWT decode + role check + redirect) was
// copy-pasted inline into every one of the 4 dashboard HTML files, with
// only the allowed-roles list differing between them. That required
// 'unsafe-inline' in the page's Content-Security-Policy script-src, which
// is a meaningfully weaker policy -- it lets ANY inline <script> execute
// on the page, not just this one trusted block, so an attacker who found
// any way to inject markup (a stored-XSS bug elsewhere, a compromised
// dependency, etc.) could run arbitrary JS too. Moving this to an
// external, same-origin file lets script-src drop 'unsafe-inline'
// entirely and rely on 'self' instead -- only scripts served from this
// app's own known files can ever run.
//
// Each page supplies only its own allowed-roles list, via a data
// attribute on the <script> tag itself (that's markup, not script, so
// the CSP doesn't restrict it) rather than a hardcoded array baked into
// this file -- keeping this one file reusable, unmodified, across every
// dashboard page. Compare against PAGE_ACCESS_RULES in js/auth.js, which
// re-checks the exact same rule again later as a defense-in-depth safety
// net once the full JS module bundle has loaded.
// =============================================================================
(function () {
  var scriptEl = document.currentScript;
  var allowedRolesAttr = scriptEl ? scriptEl.getAttribute('data-allowed-roles') : '';
  var ALLOWED_ROLES = (allowedRolesAttr || '')
    .split(',')
    .map(function (r) { return r.trim(); })
    .filter(Boolean);

  // Minimal, self-contained copy of auth.js's JWT decoder. We can't just
  // import a function from the JS modules in js/ here because those load
  // as an ES module at the very end of <body> -- by the time that runs,
  // the browser would already have parsed and painted this entire page.
  function parseJwt(token) {
    try {
      var base64 = token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
      return JSON.parse(decodeURIComponent(atob(base64).split('').map(function (c) {
        return '%' + ('00' + c.charCodeAt(0).toString(16)).slice(-2);
      }).join('')));
    } catch (e) {
      return null;
    }
  }

  function clearSessionFlag() {
    localStorage.removeItem('session');
    localStorage.removeItem('token');
  }

  function getSessionFromStorage() {
    var storedSession = localStorage.getItem('session');
    if (!storedSession) return null;
    try {
      var session = JSON.parse(storedSession);
      var expired = session.expires_at && session.expires_at * 1000 < Date.now();
      if (expired) {
        clearSessionFlag();
        return null;
      }
      return session;
    } catch (e) {
      clearSessionFlag();
      return null;
    }
  }

  var session = getSessionFromStorage();
  var payload = session || null;

  if (!ALLOWED_ROLES.length || !payload || ALLOWED_ROLES.indexOf(payload.role) === -1) {
    // location.replace (not .href) so this doesn't leave the blocked
    // dashboard page in browser history for a "back button" flash.
    window.location.replace('index.html');
  } else {
    fetch('/api/auth/me', { credentials: 'include' }).then(function (response) {
      if (!response.ok) {
        clearSessionFlag();
        window.location.replace('index.html');
        return;
      }
      // Only NOW do we allow <body> to become visible (see css/auth-guard.css).
      document.documentElement.classList.add('authorized');
    }).catch(function () {
      clearSessionFlag();
      window.location.replace('index.html');
    });
  }
})();
