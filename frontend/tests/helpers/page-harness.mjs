// =============================================================================
// frontend/tests/helpers/page-harness.mjs
// -----------------------------------------------------------------------------
// Shared loader for every "does this page actually parse and run" test in
// this directory. One deliberate design choice explains most of this file:
//
//   We do NOT let jsdom fetch <script src="..."> itself. jsdom can only do
//   that with `resources: "usable"` + a real resource loader, which means
//   real network/file fetches racing against the test and a bunch of noisy
//   jsdom-internal machinery we don't control. Instead we parse the HTML
//   with `runScripts: "outside-only"` (gives us a real `window`/`document`
//   and `dom.window.eval`, but doesn't try to auto-run anything), then do
//   the "run each script" step ourselves:
//     - classic scripts (js/theme-init.js, js/auth-guard.js): read the file
//       off disk and `dom.window.eval()` it synchronously, exactly like a
//       browser executing a render-blocking <script src> tag one at a time
//       in document order.
//     - the module script (js/main.js): dynamic `import()` of the real file
//       off disk, exactly like a browser resolving a <script type="module">
//       -- if ANY named import anywhere in its ~20-module graph doesn't
//       actually exist as an export, THIS is where it throws.
//
// This is what actually caught the "?" login bug in the first place -- see
// login-form-wiring.test.mjs's own header comment for the incident this
// harness generalizes to every page, not just index.html.
//
// FRONTEND_DIR: every test that uses this harness accepts an override via
// the FRONTEND_DIR env var (see build-pipeline.test.mjs), so the exact same
// checks can run against either the real frontend/ source tree OR a
// build-frontend/build.js output directory (local/development/production)
// -- catching regressions the build/obfuscation step itself introduces,
// not just ones already present in source.
// =============================================================================

import { readFileSync, existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { JSDOM } from 'jsdom';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// Default: the real, unbuilt source tree (frontend/, one level up from
// tests/). Overridable so the same harness can point at a build.js output
// dir instead -- see build-pipeline.test.mjs.
export function defaultFrontendDir() {
  return path.resolve(__dirname, '..', '..');
}

export function resolveFrontendDir(override) {
  return path.resolve(override || process.env.FRONTEND_DIR || defaultFrontendDir());
}

// The 5 real pages this app ships, and (for the 4 gated dashboards) the
// data-allowed-roles auth-guard.js reads off its own <script> tag. Kept as
// one list here so every test file iterates the exact same set instead of
// each hand-maintaining its own copy that could quietly drift.
export const PAGES = [
  { file: 'index.html', gated: false },
  { file: 'admin.html', gated: true },
  { file: 'staff.html', gated: true },
  { file: 'manager.html', gated: true },
  { file: 'customer.html', gated: true },
];

// Matches every <script ...> tag so callers can tell classic scripts
// (js/theme-init.js, js/auth-guard.js) apart from the one module script
// (js/main.js) and run each the way a browser actually would.
const SCRIPT_TAG_RE = /<script\b([^>]*)>/gi;
const SRC_ATTR_RE = /\bsrc\s*=\s*"([^"]+)"|\bsrc\s*=\s*'([^']+)'/i;
const TYPE_MODULE_RE = /\btype\s*=\s*["']module["']/i;

function extractScriptTags(html) {
  const tags = [];
  let match;
  while ((match = SCRIPT_TAG_RE.exec(html)) !== null) {
    const attrs = match[1];
    const srcMatch = SRC_ATTR_RE.exec(attrs);
    if (!srcMatch) continue; // inline scripts -- none exist in this app, but skip harmlessly if one shows up
    tags.push({
      src: srcMatch[1] || srcMatch[2],
      isModule: TYPE_MODULE_RE.test(attrs),
    });
  }
  return tags;
}

/**
 * Loads one page's real HTML off disk, runs its classic scripts and
 * imports its module script exactly as a browser would, then fires
 * DOMContentLoaded. Returns { dom, window, document, errors }, where
 * `errors` collects any exception thrown while running a classic script
 * or importing the module script -- an empty array means the page's JS
 * loaded exactly as cleanly as it would in a real browser.
 *
 * @param {string} pageFile e.g. "index.html"
 * @param {object} [opts]
 * @param {string} [opts.frontendDir] override for FRONTEND_DIR/default
 * @param {string} [opts.cacheBust] appended to the main.js import URL so
 *   re-loading the same page in a later test in the same process doesn't
 *   hit Node's ESM module cache and reuse a previous test's stale
 *   `document`/`window` closure (see login-form-wiring.test.mjs for the
 *   same issue).
 */
export async function loadPage(pageFile, opts = {}) {
  const frontendDir = resolveFrontendDir(opts.frontendDir);
  const pagePath = path.join(frontendDir, pageFile);
  if (!existsSync(pagePath)) {
    throw new Error(`page not found: ${pagePath} (FRONTEND_DIR=${frontendDir})`);
  }
  const html = readFileSync(pagePath, 'utf8');

  const dom = new JSDOM(html, {
    url: `http://localhost/${pageFile}`,
    runScripts: 'outside-only',
    pretendToBeVisual: true,
  });

  const { window } = dom;
  const errors = [];

  // Minimal browser globals main.js's import graph touches. Deliberately
  // NOT real fetch -- a rejected fetch is what "backend unreachable"
  // actually looks like, and every call site in the app already has a
  // try/catch for it (loadPublicConfig, checkAccess, ...). What this
  // harness cares about is whether the JS *loads and runs at all*, not
  // whether a real backend answers it. jsdom implements window.localStorage
  // for real (backed by an in-memory store per jsdom instance), so no stub
  // needed there.
  window.fetch = async () => { throw new Error('network disabled in test harness'); };
  window.alert = () => {};

  // main.js's module graph is imported via Node's own ESM loader, which
  // runs against globalThis -- NOT dom.window -- so these have to be in
  // place (including the fetch/alert stubs just above) before we ever
  // call import() below, not left for the caller to set afterward.
  // installGlobalsFor() is also exported standalone for any test that
  // needs to (re)point globalThis at a specific dom without going through
  // loadPage() again.
  installGlobalsFor(dom);

  const tags = extractScriptTags(html);

  for (const tag of tags) {
    // Only same-origin, relative script paths are ours to execute --
    // there are none pointing anywhere else in this app, but skip
    // defensively rather than trying to fetch an absolute/external URL.
    if (/^https?:\/\//i.test(tag.src)) continue;

    const scriptPath = path.join(frontendDir, tag.src);
    if (!existsSync(scriptPath)) {
      errors.push(new Error(`${pageFile}: <script src="${tag.src}"> does not exist on disk (${scriptPath})`));
      continue;
    }

    if (tag.isModule) {
      const url = pathToFileURL(scriptPath).href + (opts.cacheBust ? `?t=${opts.cacheBust}` : '');
      // The module graph's top-level code (main.js's own body, plus every
      // module it imports) runs against Node's OWN globalThis, not
      // dom.window -- ES module imports can't be pointed at an arbitrary
      // `window` the way dom.window.eval() can. So the module graph reads
      // `document`/`window`/etc. off globalThis, meaning callers must set
      // those globals (see pages-load.test.mjs) before calling loadPage().
      try {
        await import(url);
      } catch (err) {
        errors.push(new Error(`${pageFile}: failed to import module script "${tag.src}": ${err.message}`, { cause: err }));
      }
    } else {
      const source = readFileSync(scriptPath, 'utf8');
      try {
        window.eval(source);
      } catch (err) {
        errors.push(new Error(`${pageFile}: classic script "${tag.src}" threw while executing: ${err.message}`, { cause: err }));
      }
    }
  }

  window.document.dispatchEvent(new window.Event('DOMContentLoaded', { bubbles: true, cancelable: true }));

  return { dom, window, document: window.document, errors, frontendDir };
}

/**
 * Sets the handful of globalThis properties main.js's module graph reads
 * directly (not via `window.`) so a dynamic import() of it behaves like
 * a real browser's module script. Call this BEFORE loadPage() for any
 * page whose module script needs to see this particular `document`.
 */
export function installGlobalsFor(dom) {
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.localStorage = dom.window.localStorage;
  globalThis.HTMLElement = dom.window.HTMLElement;
  globalThis.CustomEvent = dom.window.CustomEvent;
  globalThis.Event = dom.window.Event;
  globalThis.fetch = dom.window.fetch;
  globalThis.alert = dom.window.alert;
}
