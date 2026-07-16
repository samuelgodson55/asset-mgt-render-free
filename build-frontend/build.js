#!/usr/bin/env node
/**
 * build-frontend/build.js
 * -----------------------------------------------------------------------
 * Processes every frontend/js/**\/*.js file differently depending on the
 * BUILD_ENV it's run with. This is what nginx/Dockerfile's builder stage
 * runs before the plain nginx stage copies the result into the image --
 * see that file for how BUILD_ENV gets wired in from .env's ENVIRONMENT
 * value via docker-compose.yml's `build.args`.
 *
 * Three modes, matching the project's three ENVIRONMENT values:
 *
 *   local        -- no processing at all. Files are copied byte-for-byte.
 *                    This is the "personal PC" mode: what ships is exactly
 *                    what's on disk, so browser DevTools line numbers match
 *                    your editor and you can breakpoint real source.
 *
 *   development  -- minified only (Terser: whitespace/comments stripped,
 *                    dead code folded, identifiers shortened). Smaller
 *                    payloads for a shared dev/staging deploy, but still
 *                    straightforward to read in DevTools if you need to.
 *
 *   production   -- minified AND obfuscated. Terser runs first (same as
 *                    development), then javascript-obfuscator runs on the
 *                    result (string-array encoding, control-flow
 *                    flattening, dead-code injection, self-defending
 *                    output) so the shipped bundle is deliberately hard to
 *                    read or tamper with, not just small.
 *
 * USAGE
 *   BUILD_ENV=local|development|production node build.js <srcDir> <outDir>
 *
 * Everything under <srcDir> that ISN'T a .js file (html, css, images,
 * fonts, ...) is copied through unchanged in every mode -- only .js files
 * are ever transformed. Prints a per-file + total size report to stdout
 * either way, so `docker compose build` output shows exactly what
 * happened for the mode you just built.
 * -----------------------------------------------------------------------
 */

const fs = require("fs");
const path = require("path");
const { minify } = require("terser");
const JavaScriptObfuscator = require("javascript-obfuscator");

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
  development: "minify",
  production: "minify + obfuscate",
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

  const minified = await minify(code, {
    compress: true,
    mangle: true,
    format: { comments: false },
  });
  if (minified.error) {
    throw new Error(`terser failed on ${relPath}: ${minified.error}`);
  }
  let output = minified.code || "";

  if (mode === "production") {
    const obfuscated = JavaScriptObfuscator.obfuscate(output, {
      compact: true,
      controlFlowFlattening: true,
      controlFlowFlatteningThreshold: 0.75,
      deadCodeInjection: true,
      deadCodeInjectionThreshold: 0.4,
      stringArray: true,
      stringArrayEncoding: ["base64"],
      stringArrayThreshold: 0.75,
      identifierNamesGenerator: "hexadecimal",
      selfDefending: true,
      renameGlobals: false,
      disableConsoleOutput: false,
    });
    output = obfuscated.getObfuscatedCode();
  }

  return output;
}

async function main() {
  if (!fs.existsSync(srcDir)) {
    fail(`source dir does not exist: ${srcDir}`);
  }
  fs.mkdirSync(outDir, { recursive: true });

  const files = walk(srcDir);
  const jsFiles = files.filter((f) => f.endsWith(".js"));
  const otherFiles = files.filter((f) => !f.endsWith(".js"));

  console.log("=".repeat(72));
  console.log(`Snipe-IT Lite frontend build -- BUILD_ENV=${mode}`);
  console.log(`Mode: ${MODE_LABEL[mode]}`);
  console.log(`Source: ${srcDir}`);
  console.log(`Output: ${outDir}`);
  console.log("=".repeat(72));

  // Non-JS assets: copy through untouched in every mode.
  for (const file of otherFiles) {
    const rel = path.relative(srcDir, file);
    const dest = path.join(outDir, rel);
    fs.mkdirSync(path.dirname(dest), { recursive: true });
    fs.copyFileSync(file, dest);
  }

  let totalBefore = 0;
  let totalAfter = 0;
  const rows = [];

  for (const file of jsFiles) {
    const rel = path.relative(srcDir, file);
    const dest = path.join(outDir, rel);
    fs.mkdirSync(path.dirname(dest), { recursive: true });

    const before = fs.readFileSync(file, "utf8");
    const after = await processJs(before, rel);
    fs.writeFileSync(dest, after, "utf8");

    const beforeBytes = Buffer.byteLength(before, "utf8");
    const afterBytes = Buffer.byteLength(after, "utf8");
    totalBefore += beforeBytes;
    totalAfter += afterBytes;
    rows.push({ rel, beforeBytes, afterBytes });
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
    `  ${jsFiles.length} JS file(s) processed, ${otherFiles.length} other asset(s) copied as-is.`
  );
  console.log("=".repeat(72));
}

main().catch((err) => {
  fail(err.stack || String(err));
});
