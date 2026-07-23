// =============================================================================
// frontend/tests/login-form-wiring.test.mjs
// -----------------------------------------------------------------------------
// Regression test for the bug reported as "login keeps showing a bare '?' on
// the address bar and never actually logs in".
//
// ROOT CAUSE (see CHANGELOG-worthy incident): `frontend/index.html` (and
// every other page) loads exactly one script -- `<script type="module"
// src="js/main.js">` -- which statically imports every other frontend
// module, including named exports like `openRevokeUserModal` from
// `components/users.js`. ES modules are linked (all imports resolved)
// BEFORE any code in the graph runs. If main.js imports a name that a
// component module doesn't actually export, the browser throws a
// SyntaxError/TypeError at load time for the ENTIRE module graph -- not
// just the one broken feature. That means NOTHING in main.js ever runs,
// including the `DOMContentLoaded` handler that wires up
// `#login-form`'s `submit` listener (the one that calls `e.preventDefault()`
// and POSTs to `/api/auth/login`).
//
// With no JS ever attaching that listener, clicking "Sign In" falls back to
// the browser's own native form submission. `#login-form` has no `action`/
// `method` set, so the default is a GET back to the current page with an
// (empty, since the inputs have no `name` attributes) query string --
// exactly the `localhost:8080/?` reload the user saw. The backend is never
// even contacted.
//
// THIS TEST catches that whole class of regression directly and cheaply:
// it loads the real index.html into jsdom, dynamically imports the real
// js/main.js (no mocking of app modules -- if ANY export anywhere in the
// import graph is missing, `import()` itself rejects here exactly like the
// browser would fail), fires DOMContentLoaded, then dispatches a real
// 'submit' event on #login-form and asserts the JS actually intercepted it
// (`event.defaultPrevented === true`). If main.js ever again fails to load
// -- for ANY reason, anywhere in its ~20-module import graph -- this test
// fails with the real underlying import error instead of a mysterious
// "login is broken" bug report.
// =============================================================================

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';
import { JSDOM } from 'jsdom';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FRONTEND_DIR = path.resolve(__dirname, '..');
const INDEX_HTML_PATH = path.join(FRONTEND_DIR, 'index.html');
const MAIN_JS_URL = pathToFileURL(path.join(FRONTEND_DIR, 'js', 'main.js')).href;

// Sets up a fresh jsdom `document`/`window` from the REAL index.html (not a
// hand-rolled stand-in), plus the minimal browser globals main.js's import
// graph touches (fetch, localStorage, alert) -- stubbed just enough that
// unrelated network/UI calls (loadPublicConfig, checkAccess, etc.) fail
// gracefully into their existing try/catch blocks instead of throwing
// somewhere unrelated to what this test is actually checking.
function installBrowserGlobals() {
  const html = readFileSync(INDEX_HTML_PATH, 'utf8');
  const dom = new JSDOM(html, { url: 'http://localhost:8080/' });

  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.localStorage = dom.window.localStorage;
  globalThis.HTMLElement = dom.window.HTMLElement;
  globalThis.CustomEvent = dom.window.CustomEvent;
  globalThis.Event = dom.window.Event;

  // Every apiRequest()/login() call the boot sequence might make (e.g.
  // loadPublicConfig(), checkAccess()) is already wrapped in a try/catch in
  // the app code -- a rejected fetch is a realistic "API unreachable"
  // condition, not something this test needs to fake success for.
  globalThis.fetch = async () => { throw new Error('network disabled in test'); };

  // jsdom's window.alert is "not implemented" and logs noisily to the
  // virtual console by default -- silence it, the same way a real browser's
  // alert() just pauses and returns, it doesn't crash anything.
  dom.window.alert = () => {};

  return dom;
}

test('main.js loads without an import/link error (the exact failure mode behind the "?" login bug)', async () => {
  installBrowserGlobals();

  // If ANY named import anywhere in main.js's ~20-module graph doesn't
  // exist as an export (the actual root cause of the reported bug), this
  // rejects here with the same "does not provide an export named ..."
  // error the browser would surface -- instead of the far more confusing
  // "login silently reloads to '?'" symptom a user sees.
  await assert.doesNotReject(
    () => import(MAIN_JS_URL),
    /.*/,
    'js/main.js failed to import -- this breaks EVERY page\'s JS, including the login form\'s submit handler'
  );
});

test('the login form\'s submit is intercepted by JS instead of falling back to a native page reload', async () => {
  installBrowserGlobals();
  // Cache-bust: Node's ESM loader caches modules by exact URL, and
  // main.js's `document.addEventListener('DOMContentLoaded', ...)` runs
  // once at module-evaluation time, capturing whatever `document` was
  // the global at that moment. Re-importing the identical URL from an
  // earlier test would just return the cached module bound to that
  // earlier test's (now-stale) document, not this test's fresh one.
  await import(`${MAIN_JS_URL}?test=login-form-wiring`);

  // main.js's real wiring lives inside a DOMContentLoaded listener --
  // fire it exactly like the browser does once the document is parsed.
  document.dispatchEvent(new window.Event('DOMContentLoaded', { bubbles: true, cancelable: true }));

  const loginForm = document.getElementById('login-form');
  assert.ok(loginForm, 'index.html should still have a #login-form element');

  // Fill in the fields the submit handler reads, same as a real user typing.
  document.getElementById('login-email').value = 'r.adeyemi@corp.io';
  document.getElementById('login-password').value = 'Admin123!';

  const submitEvent = new window.Event('submit', { bubbles: true, cancelable: true });
  loginForm.dispatchEvent(submitEvent);

  // The real bug: with main.js broken, NO listener is attached, so
  // dispatching 'submit' does nothing and `defaultPrevented` stays false --
  // in a real browser that's exactly the case where the native GET
  // submission fires and the address bar ends up as `.../?`. Once main.js
  // loads correctly, its handler's very first line is `e.preventDefault()`,
  // which runs synchronously before the (mocked, rejecting) login API call
  // is even awaited.
  assert.equal(
    submitEvent.defaultPrevented,
    true,
    'submitting the login form should be intercepted by JS (e.preventDefault()) instead of falling back to a native browser form submission'
  );
});
