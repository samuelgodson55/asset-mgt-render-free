// =============================================================================
// frontend/tests/pages-load.test.mjs
// -----------------------------------------------------------------------------
// Generalizes login-form-wiring.test.mjs's core insight -- "load the REAL
// HTML/JS off disk, run it the way a browser would, and see if it throws" --
// to every page this app ships (index/admin/staff/manager/customer.html),
// plus a few cheap static checks that catch the other common ways a
// frontend silently breaks in production without a stack trace anyone
// notices until a user reports "the page just doesn't do anything":
//
//   1. Duplicate `id` attributes -- getElementById() only ever returns the
//      FIRST match, so a second element with the same id is invisible to
//      every listener/lookup that targets it. Easy to introduce by
//      copy-pasting a modal/row template and forgetting to change the id.
//   2. Broken local asset references -- a <script src>, <link href>, or
//      <img src> pointing at a file that doesn't exist on disk. In dev
//      this is a loud 404 in the Network tab; after html-minifier-terser
//      + javascript-obfuscator run in CI/prod builds, a typo'd path is
//      just as broken but far less obvious to spot by eye in a diff.
//   3. Script execution errors -- the actual browser failure mode behind
//      the "?" login bug: any classic script (theme-init.js, auth-guard.js)
//      throwing, or the module script (main.js) failing to import because
//      something in its ~20-module graph doesn't line up, silently kills
//      ALL of that page's JS, including form submit handlers.
//
// Every check here accepts FRONTEND_DIR overrides via the shared harness,
// so build-pipeline.test.mjs can run the exact same assertions against a
// built (minified/obfuscated) output directory, not just raw source.
// =============================================================================

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';
import path from 'node:path';
import { loadPage, resolveFrontendDir, PAGES } from './helpers/page-harness.mjs';

const LOCAL_ATTR_RE = {
  script: /<script\b[^>]*\bsrc\s*=\s*["']([^"']+)["'][^>]*>/gi,
  link: /<link\b[^>]*\brel\s*=\s*["']stylesheet["'][^>]*\bhref\s*=\s*["']([^"']+)["'][^>]*>|<link\b[^>]*\bhref\s*=\s*["']([^"']+)["'][^>]*\brel\s*=\s*["']stylesheet["'][^>]*>/gi,
  img: /<img\b[^>]*\bsrc\s*=\s*["']([^"']+)["'][^>]*>/gi,
};

function extractLocalRefs(html, re) {
  const refs = [];
  let match;
  re.lastIndex = 0;
  while ((match = re.exec(html)) !== null) {
    const ref = match[1] || match[2];
    if (ref && !/^https?:\/\//i.test(ref) && !ref.startsWith('data:')) {
      refs.push(ref);
    }
  }
  return refs;
}

for (const { file } of PAGES) {
  test(`${file}: every local script/stylesheet/image reference exists on disk`, () => {
    const frontendDir = resolveFrontendDir();
    const html = readFileSync(path.join(frontendDir, file), 'utf8').replace(/<!--[\s\S]*?-->/g, '');

    const refs = [
      ...extractLocalRefs(html, LOCAL_ATTR_RE.script),
      ...extractLocalRefs(html, LOCAL_ATTR_RE.link),
      ...extractLocalRefs(html, LOCAL_ATTR_RE.img),
    ];

    assert.ok(refs.length > 0, `${file}: expected to find at least one local asset reference to check -- did the markup change shape?`);

    const missing = refs.filter((ref) => !existsSync(path.join(frontendDir, ref)));
    assert.deepEqual(missing, [], `${file}: these referenced local files do not exist on disk: ${missing.join(', ')}`);
  });

  test(`${file}: no duplicate element ids`, () => {
    const frontendDir = resolveFrontendDir();
    const html = readFileSync(path.join(frontendDir, file), 'utf8').replace(/<!--[\s\S]*?-->/g, '');

    // Static regex scan rather than a full jsdom parse -- cheap, and
    // duplicate-id bugs are a markup-authoring mistake that doesn't need
    // script execution to detect.
    const ids = [...html.matchAll(/\bid\s*=\s*["']([^"']+)["']/gi)].map((m) => m[1]);
    const seen = new Set();
    const duplicates = new Set();
    for (const id of ids) {
      if (seen.has(id)) duplicates.add(id);
      seen.add(id);
    }
    assert.deepEqual(
      [...duplicates],
      [],
      `${file}: duplicate id attribute(s) found -- getElementById()/querySelector('#id') will only ever see the first one, silently breaking any code targeting the others`
    );
  });

  test(`${file}: page's scripts execute without throwing (classic scripts run + module script imports cleanly)`, async () => {
    const { errors } = await loadPage(file, { cacheBust: `pages-load-${file}-${Date.now()}` });
    assert.deepEqual(
      errors.map((e) => e.message),
      [],
      `${file}: one or more scripts failed -- when this happens in a real browser, EVERY handler on the page (including any form's submit listener) silently never gets wired up`
    );
  });
}
