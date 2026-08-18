#!/usr/bin/env node
/**
 * build-frontend/build.js
 * -----------------------------------------------------------------------
 * Processes every frontend/js/**\/*.js AND every frontend/*.html file
 * differently depending on the BUILD_ENV it's run with. This is what
 * frontend/Dockerfile's builder stage runs before the plain nginx stage
 * copies the result into the image -- see that file for how BUILD_ENV
 * gets wired in from .env's ENVIRONMENT value via docker-compose.yml's
 * `build.args`.
 *
 * Three modes, matching the project's three ENVIRONMENT values:
 *
 *   local        -- no processing at all. Files are copied byte-for-byte.
 *                    This is the "personal PC" mode: what ships is exactly
 *                    what's on disk, so browser DevTools line numbers match
 *                    your editor and you can breakpoint real source.
 *
 *   development  -- JS minified (Terser: whitespace/comments stripped,
 *                    dead code folded, identifiers shortened); HTML
 *                    minified (html-minifier-terser: whitespace collapsed,
 *                    comments stripped). Smaller payloads for a shared
 *                    dev/staging deploy, but still straightforward to
 *                    read in DevTools if you need to.
 *
 *   production   -- everything development does, PLUS the minified JS is
 *                    run through javascript-obfuscator (string-array
 *                    encoding, control-flow flattening, dead-code
 *                    injection, self-defending output) so the shipped
 *                    bundle is deliberately hard to read or tamper with,
 *                    not just small. HTML minification is the same as
 *                    development -- HTML isn't obfuscated, just minified,
 *                    since every <script> tag here loads an external .js
 *                    file (no inline JS to protect) and there's nothing
 *                    equivalent to "obfuscation" for markup. PLUS: any
 *                    HTML block wrapped in
 *                      <!-- BUILD:PROD-STRIP:START -->
 *                      ...
 *                      <!-- BUILD:PROD-STRIP:END -->
 *                    is deleted outright (e.g. frontend/index.html's demo
 *                    account credentials box) -- see stripProdOnlyBlocks()
 *                    below. Still present, unmodified, in local and
 *                    development builds.
 *
 * USAGE
 *   BUILD_ENV=local|development|production node build.js <srcDir> <outDir>
 *
 * Everything under <srcDir> that ISN'T a .js or .html file (css, images,
 * fonts, ...) is copied through unchanged in every mode. Prints a
 * per-file + total size report to stdout either way, so `docker compose
 * build` output shows exactly what happened for the mode you just built.
 * -----------------------------------------------------------------------
 */

const fs = require("fs");
const path = require("path");
const { minify } = require("terser");
const JavaScriptObfuscator = require("javascript-obfuscator");
const { minify: minifyHtml } = require("html-minifier-terser");

const VALID_MODES = new Set(["local", "development", "production"]);

function fail(msg) {
  console.error(`\n[build-frontend] ERROR: ${msg}\n`);
  process.exit(1);
}

const rawMode = (process.env.BUILD_ENV || "development").trim().toLowerCase();
// "prod"/"dev" aliases tolerated since backend/config.py already accepts
// "prod" as a synonym for "production" -- keeps the two halves consistent.
const ALIASES = { prod: "production", dev: "development" };
const mode = ALIASES[rawMode] || rawMode;

if (!VALID_MODES.has(mode)) {
  fail(
    `BUILD_ENV="${rawMode}" is not one of: local, development, production.`
  );
}

const [, , srcDirArg, outDirArg] = process.argv;
if (!srcDirArg || !outDirArg) {
  fail("usage: BUILD_ENV=<mode> node build.js <srcDir> <outDir>");
}
const srcDir = path.resolve(srcDirArg);
const outDir = path.resolve(outDirArg);

const MODE_LABEL = {
  local: "none (raw source, copied as-is -- personal PC)",
  development: "minify JS + minify HTML",
  production: "minify JS + obfuscate JS + minify HTML",
};

function fmtKB(bytes) {
  return `${(bytes / 1024).toFixed(1)} KB`;
}

function fmtPct(before, after) {
  if (before === 0) return "0.0%";
  const pct = ((before - after) / before) * 100;
  return `${pct >= 0 ? "-" : "+"}${Math.abs(pct).toFixed(1)}%`;
}

function walk(dir) {
  let results = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      results = results.concat(walk(full));
    } else {
      results.push(full);
    }
  }
  return results;
}

async function processJs(code, relPath) {
  if (mode === "local") {
    return code;
  }

  let minified;
  try {
    minified = await minify(code, {
      compress: true,
      mangle: true,
      format: { comments: false },
    });
  } catch (err) {
    // Newer terser versions REJECT the promise (throw) on a hard parse
    // failure instead of resolving with `.error` set -- the `if
    // (minified.error)` check below only ever caught the latter case, so
    // a rejection used to propagate terser's own internal stack trace
    // (bundle.min.js:NNNN) all the way up to main().catch() with nothing
    // in the log identifying which of OUR files caused it. Re-throwing
    // here with relPath prefixed makes that always show up regardless of
    // which failure mode terser used.
    throw new Error(`terser failed on ${relPath}: ${err && err.message ? err.message : err}`, { cause: err });
  }
  if (minified.error) {
    throw new Error(`terser failed on ${relPath}: ${minified.error}`, { cause: minified.error });
  }
  let output = minified.code || "";

  if (mode === "production") {
    // Keep production obfuscation deliberately conservative.  The legacy
    // frontend is an event-driven multi-page application: main.js wires
    // DOMContentLoaded, submit, click, and change handlers at runtime.
    // javascript-obfuscator's control-flow flattening + dead-code injection
    // + self-defending combination has historically been able to produce
    // output that imports cleanly but changes runtime event wiring.  That is
    // exactly the dangerous failure mode behind the historical "login -> ?"
    // incident: the page looks healthy, but the submit listener never runs.
    //
    // We still obfuscate production JavaScript (compact output, hexadecimal
    // identifiers, and encoded string literals), but leave program flow and
    // function identity intact.  This gives us the security/size benefit
    // without transforming the code paths that browsers use for event
    // dispatch.  The production CI test deliberately exercises the real
    // login submit listener after this transformation.
    const obfuscated = JavaScriptObfuscator.obfuscate(output, {
      compact: true,
      controlFlowFlattening: false,
      deadCodeInjection: false,
      stringArray: true,
      stringArrayEncoding: ["base64"],
      stringArrayThreshold: 0.75,
      identifierNamesGenerator: "hexadecimal",
      selfDefending: false,
      renameGlobals: false,
      disableConsoleOutput: false,
    });
    output = obfuscated.getObfuscatedCode();
  }

  return output;
}

// Removes any <!-- BUILD:PROD-STRIP:START --> ... <!-- BUILD:PROD-STRIP:END -->
// block from an HTML source string. Only called for mode === "production"
// (see processHtml below) -- local/development builds keep these blocks
// untouched so things like frontend/index.html's demo-account credentials
// box are still visible while developing/staging, and only disappear from
// the page in a real production build. Matching is non-greedy and global
// so multiple independent blocks in the same file are all removed; a
// missing/unbalanced marker is treated as a hard build failure rather than
// silently left in place or silently deleting too much, since that almost
// certainly means a marker was typo'd or only one of the pair was pasted.
const PROD_STRIP_START = "<!-- BUILD:PROD-STRIP:START -->";
const PROD_STRIP_END = "<!-- BUILD:PROD-STRIP:END -->";
const PROD_STRIP_RE = /<!--\s*BUILD:PROD-STRIP:START\s*-->[\s\S]*?<!--\s*BUILD:PROD-STRIP:END\s*-->/g;

function stripProdOnlyBlocks(code, relPath) {
  const starts = (code.match(/<!--\s*BUILD:PROD-STRIP:START\s*-->/g) || []).length;
  const ends = (code.match(/<!--\s*BUILD:PROD-STRIP:END\s*-->/g) || []).length;
  if (starts !== ends) {
    throw new Error(
      `${relPath}: found ${starts} "${PROD_STRIP_START}" marker(s) but ` +
        `${ends} "${PROD_STRIP_END}" marker(s) -- these must come in matched ` +
        `pairs. Fix the markers before building.`
    );
  }
  return code.replace(PROD_STRIP_RE, "");
}


// loads an external .js file (verified -- no inline <script> blocks
// exist anywhere in frontend/*.html), so minifyJS is left off rather than
// risk html-minifier-terser trying to parse markup as script. Same
// reasoning for minifyCSS -- there are no inline <style> blocks either;
// real CSS lives in frontend/css/*.css and passes through untouched like
// any other non-JS/non-HTML asset. collapseWhitespace is safe here since
// none of these pages use <pre> (whitespace-significant) and every
// <textarea> in them is empty (no default text to preserve).
async function processHtml(code, relPath) {
  if (mode === "local") {
    return code;
  }

  let source = code;
  if (mode === "production") {
    source = stripProdOnlyBlocks(source, relPath);
  }

  try {
    return await minifyHtml(source, {
      collapseWhitespace: true,
      conservativeCollapse: false,
      removeComments: true,
      // html-minifier-terser's removeComments:true does NOT mean "remove
      // every comment" -- it defaults ignoreCustomComments to
      // [/^!/, /^\s*#/], protecting "important" (<!--! ... -->) comments
      // and anything shaped like a Server-Side-Include directive
      // (<!--#include file="..." -->). This app has neither -- every
      // comment in frontend/*.html is plain developer documentation, some
      // of which (e.g. "<!-- #swipeArea wraps... -->") happens to start
      // with "#" as a way of naming the element it's describing, which
      // matched that SSI-shaped default and was silently surviving every
      // build (dev AND prod) even though removeComments was already on.
      // Empty array means "no exceptions -- strip every comment".
      ignoreCustomComments: [],
      removeRedundantAttributes: false,
      removeAttributeQuotes: false,
      minifyJS: false,
      minifyCSS: false,
    });
  } catch (err) {
    throw new Error(`html-minifier-terser failed on ${relPath}: ${err.message}`);
  }
}

async function main() {
  if (!fs.existsSync(srcDir)) {
    fail(`source dir does not exist: ${srcDir}`);
  }
  fs.mkdirSync(outDir, { recursive: true });

  const files = walk(srcDir);
  const jsFiles = files.filter((f) => f.endsWith(".js"));
  const htmlFiles = files.filter((f) => f.endsWith(".html"));
  const otherFiles = files.filter(
    (f) => !f.endsWith(".js") && !f.endsWith(".html")
  );

  console.log("=".repeat(72));
  console.log(`Snipe-IT Lite frontend build -- BUILD_ENV=${mode}`);
  console.log(`Mode: ${MODE_LABEL[mode]}`);
  console.log(`Source: ${srcDir}`);
  console.log(`Output: ${outDir}`);
  console.log("=".repeat(72));

  // Everything that isn't JS or HTML (css, images, fonts, ...): copy
  // through untouched in every mode.
  for (const file of otherFiles) {
    const rel = path.relative(srcDir, file);
    const dest = path.join(outDir, rel);
    fs.mkdirSync(path.dirname(dest), { recursive: true });
    fs.copyFileSync(file, dest);
  }

  let totalBefore = 0;
  let totalAfter = 0;
  const rows = [];

  const processors = [
    { files: jsFiles, run: processJs },
    { files: htmlFiles, run: processHtml },
  ];

  for (const { files: group, run } of processors) {
    for (const file of group) {
      const rel = path.relative(srcDir, file);
      const dest = path.join(outDir, rel);
      fs.mkdirSync(path.dirname(dest), { recursive: true });

      const before = fs.readFileSync(file, "utf8");
      const after = await run(before, rel);
      fs.writeFileSync(dest, after, "utf8");

      const beforeBytes = Buffer.byteLength(before, "utf8");
      const afterBytes = Buffer.byteLength(after, "utf8");
      totalBefore += beforeBytes;
      totalAfter += afterBytes;
      rows.push({ rel, beforeBytes, afterBytes });
    }
  }

  rows.sort((a, b) => b.beforeBytes - a.beforeBytes);
  const relWidth = Math.max(...rows.map((r) => r.rel.length), 20) + 2;
  for (const r of rows) {
    console.log(
      `  ${r.rel.padEnd(relWidth)} ${fmtKB(r.beforeBytes).padStart(9)} -> ` +
        `${fmtKB(r.afterBytes).padStart(9)}  (${fmtPct(r.beforeBytes, r.afterBytes)})`
    );
  }

  console.log("-".repeat(72));
  console.log(
    `  ${"TOTAL".padEnd(relWidth)} ${fmtKB(totalBefore).padStart(9)} -> ` +
      `${fmtKB(totalAfter).padStart(9)}  (${fmtPct(totalBefore, totalAfter)})`
  );
  console.log(
    `  ${jsFiles.length} JS file(s) + ${htmlFiles.length} HTML file(s) processed, ` +
      `${otherFiles.length} other asset(s) copied as-is.`
  );
  console.log("=".repeat(72));
}

main().catch((err) => {
  fail(err.stack || String(err));
});
