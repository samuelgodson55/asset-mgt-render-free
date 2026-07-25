// =============================================================================
// frontend/tests/build-pipeline.test.mjs
// -----------------------------------------------------------------------------
// Every other test in this directory checks the real frontend/ SOURCE tree.
// That's necessary but not sufficient: build-frontend/build.js transforms
// every .js/.html file before it ever ships (minified in "development",
// minified + obfuscated with javascript-obfuscator in "production" -- see
// that script's own header comment), and a transform step can introduce a
// bug that isn't in the source at all. This file is the one place that
// actually runs the real build for each mode and re-checks the OUTPUT --
// exactly the class of regression that source-only tests can't see.
//
// Skips gracefully (not a failure) if build-frontend's own dependencies
// (terser, javascript-obfuscator, html-minifier-terser) aren't installed --
// this test suite (frontend/tests) and the build tool (build-frontend) are
// two separate npm projects with two separate `npm ci` steps (see
// .github/workflows/ci.yml), and a developer running only
// `cd frontend/tests && npm test` locally shouldn't get a confusing
// failure for a dependency tree they never installed.
//
// Also never points build.js at the real frontend/ directory as-is --
// see copyFrontendSourceExcludingTests() below for why.
// =============================================================================

import test from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync, existsSync, cpSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { loadPage, PAGES } from './helpers/page-harness.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = path.resolve(__dirname, '..', '..');
const FRONTEND_SRC = path.join(PROJECT_ROOT, 'frontend');
const BUILD_SCRIPT = path.join(PROJECT_ROOT, 'build-frontend', 'build.js');
const BUILD_FRONTEND_NODE_MODULES = path.join(PROJECT_ROOT, 'build-frontend', 'node_modules');

const BUILD_MODES = ['local', 'development', 'production'];

// Mirrors exactly what `docker build` does with the root .dockerignore in
// place: everything in frontend/ EXCEPT frontend/tests/ (this test suite
// itself, including its own installed node_modules) reaches the build
// step. Copying into a scratch dir rather than pointing build.js at
// FRONTEND_SRC directly matters here specifically because this test suite
// lives inside frontend/tests/ -- pointing build.js at the real frontend/
// on a machine that has already run `npm ci` in tests/ would make it walk
// (and choke on) tests/node_modules/**, which is exactly the bug the
// .dockerignore entry exists to prevent in the real image build.
function copyFrontendSourceExcludingTests(destDir) {
  cpSync(FRONTEND_SRC, destDir, {
    recursive: true,
    filter: (src) => {
      const rel = path.relative(FRONTEND_SRC, src);
      return rel !== 'tests' && !rel.startsWith(`tests${path.sep}`);
    },
  });
}

function buildInto(mode, outDir) {
  const scratchSrc = mkdtempSync(path.join(tmpdir(), `snipeit-frontend-src-${mode}-`));
  try {
    copyFrontendSourceExcludingTests(scratchSrc);
    execFileSync(
      process.execPath,
      [BUILD_SCRIPT, scratchSrc, outDir],
      {
        cwd: PROJECT_ROOT,
        env: { ...process.env, BUILD_ENV: mode },
        stdio: 'pipe', // build.js's per-file size report is noise here; surfaced on failure below
      }
    );
  } finally {
    rmSync(scratchSrc, { recursive: true, force: true });
  }
}

for (const mode of ['development', 'production']) {
  test(`BUILD_ENV=${mode}: built HTML has every comment stripped (no "#..."/"!..." comment survives html-minifier-terser's ignoreCustomComments default)`, { skip: !existsSync(BUILD_FRONTEND_NODE_MODULES) && 'build-frontend/node_modules not installed -- run `npm ci` in build-frontend/ first' }, () => {
    // Regression test for a real bug: html-minifier-terser's
    // removeComments:true does NOT strip every comment by default -- it
    // defaults `ignoreCustomComments` to [/^!/, /^\s*#/], silently
    // preserving any comment whose content starts with "#" (meant to
    // protect Server-Side-Include directives like <!--#include ...-->,
    // which this app has none of) or "!" (an "important comment"
    // convention). This codebase's own documentation style -- e.g.
    // "<!-- #swipeArea wraps... -->" in admin.html/manager.html,
    // "<!-- #dashSwipeArea ... -->" / "<!-- #quotationSwipeArea ... -->"
    // in staff.html/customer.html -- happened to match that pattern and
    // was leaking into every built page (dev AND prod) until build.js
    // explicitly set `ignoreCustomComments: []`. See that option's own
    // comment in build.js for the full explanation.
    const outDir = mkdtempSync(path.join(tmpdir(), `snipeit-frontend-build-${mode}-comments-`));
    try {
      buildInto(mode, outDir);
      for (const { file } of PAGES) {
        const html = readFileSync(path.join(outDir, file), 'utf8');
        const comments = html.match(/<!--[\s\S]*?-->/g) || [];
        assert.deepEqual(
          comments,
          [],
          `BUILD_ENV=${mode} ${file} still has ${comments.length} HTML comment(s) after minification -- html-minifier-terser's ignoreCustomComments default may have crept back in`
        );
      }
    } finally {
      rmSync(outDir, { recursive: true, force: true });
    }
  });
}

for (const mode of BUILD_MODES) {
  test(`BUILD_ENV=${mode}: build.js produces output whose pages load without a script error`, { skip: !existsSync(BUILD_FRONTEND_NODE_MODULES) && 'build-frontend/node_modules not installed -- run `npm ci` in build-frontend/ first (CI does this automatically)' }, async () => {
    const outDir = mkdtempSync(path.join(tmpdir(), `snipeit-frontend-build-${mode}-`));
    try {
      assert.doesNotThrow(
        () => buildInto(mode, outDir),
        `build-frontend/build.js itself failed for BUILD_ENV=${mode} -- see stderr above`
      );

      // index.html covers the login form specifically (see
      // login-form-wiring.test.mjs); every page shares the same js/main.js,
      // so one representative page per mode is enough to catch a build
      // step that broke the module graph, without tripling this already
      // build-heavy test's runtime for no extra signal.
      const { errors } = await loadPage('index.html', {
        frontendDir: outDir,
        cacheBust: `build-pipeline-${mode}-${Date.now()}`,
      });
      assert.deepEqual(
        errors.map((e) => e.message),
        [],
        `BUILD_ENV=${mode} output has a broken script -- the build/minify/obfuscate step introduced this, it isn't in frontend/ source`
      );
    } finally {
      rmSync(outDir, { recursive: true, force: true });
    }
  });
}

test('BUILD_ENV=production: the built login form is still intercepted by JS (not a native page reload)', { skip: !existsSync(BUILD_FRONTEND_NODE_MODULES) && 'build-frontend/node_modules not installed -- run `npm ci` in build-frontend/ first' }, async () => {
  const outDir = mkdtempSync(path.join(tmpdir(), 'snipeit-frontend-build-production-loginform-'));
  try {
    buildInto('production', outDir);

    const { window, document, errors } = await loadPage('index.html', {
      frontendDir: outDir,
      cacheBust: `build-pipeline-production-loginform-${Date.now()}`,
    });
    assert.deepEqual(errors.map((e) => e.message), [], 'production build had a script error before we even got to submitting the form');

    const loginForm = document.getElementById('login-form');
    assert.ok(loginForm, 'production build\'s index.html should still have a #login-form element');

    document.getElementById('login-email').value = 'r.adeyemi@corp.io';
    document.getElementById('login-password').value = 'Admin123!';

    const submitEvent = new window.Event('submit', { bubbles: true, cancelable: true });
    loginForm.dispatchEvent(submitEvent);

    assert.equal(
      submitEvent.defaultPrevented,
      true,
      'the obfuscated production build should still intercept the login form submit with JS -- if this is false, the obfuscation step (selfDefending/controlFlowFlattening/etc.) broke event wiring even though the module "imported" without throwing'
    );
  } finally {
    rmSync(outDir, { recursive: true, force: true });
  }
});
