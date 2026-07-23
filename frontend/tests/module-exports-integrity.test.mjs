// =============================================================================
// frontend/tests/module-exports-integrity.test.mjs
// -----------------------------------------------------------------------------
// pages-load.test.mjs / login-form-wiring.test.mjs catch a broken import
// graph DYNAMICALLY -- by actually importing main.js and seeing whether it
// throws. That's the most realistic check (it's exactly what a browser
// does), but when it fails it only reports the FIRST broken import Node's
// loader happens to hit, and only for modules actually reachable from
// main.js's own entry point.
//
// This file complements that with a STATIC, execution-free sweep: parse
// every import statement in every frontend/js/**/*.js file, resolve the
// module it points at, and confirm every named binding it asks for is
// actually declared as an export there. No jsdom, no module execution, no
// network -- just text analysis, so it also:
//   - reports EVERY broken import in one run, not just the first
//   - catches an unused/orphaned module's broken import too, even if
//     nothing currently reachable from main.js happens to import it
//
// Deliberately regex-based rather than a full AST parse: this codebase's
// own export styles are consistently simple (verified by grep across every
// file before writing this) -- `export function foo(...)`, `export async
// function foo(...)`, `export const/let/var foo`, `export class Foo`, and
// exactly one `export default` (vendor/qrcode.js). There is no
// `export { a, b } from './x.js'` re-export anywhere in this codebase
// (also verified) -- if one is ever added, EXPORT_RE below needs a
// matching case added or this test will under-report for that file.
// =============================================================================

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { resolveFrontendDir } from './helpers/page-harness.mjs';
import { walkJsFiles } from './helpers/walk-js-files.mjs';
import { stripComments } from './helpers/strip-comments.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// Matches `import ... from '...'` / `"..."`, across multiple lines (named
// import lists in this codebase are frequently wrapped over several lines
// -- see main.js), non-greedy so it stops at the first closing quote+`from`.
const IMPORT_RE = /import\s+([^;]+?)\s+from\s+['"]([^'"]+)['"]\s*;/g;

// Splits one import clause (everything between `import` and `from`) into
// { defaultName, namespaceName, named: [{ imported, local }] }.
function parseImportClause(clause) {
  const result = { defaultName: null, namespaceName: null, named: [] };
  let rest = clause.trim();

  const namespaceMatch = rest.match(/^\*\s+as\s+([a-zA-Z0-9_$]+)$/);
  if (namespaceMatch) {
    result.namespaceName = namespaceMatch[1];
    return result;
  }

  const namedBlockMatch = rest.match(/\{([\s\S]*)\}/);
  if (namedBlockMatch) {
    const before = rest.slice(0, namedBlockMatch.index).replace(/,\s*$/, '').trim();
    if (before) result.defaultName = before;

    const body = namedBlockMatch[1];
    for (const part of body.split(',')) {
      const trimmed = part.trim();
      if (!trimmed) continue;
      const asMatch = trimmed.match(/^([a-zA-Z0-9_$]+)\s+as\s+([a-zA-Z0-9_$]+)$/);
      if (asMatch) {
        result.named.push({ imported: asMatch[1], local: asMatch[2] });
      } else {
        result.named.push({ imported: trimmed, local: trimmed });
      }
    }
  } else if (rest) {
    result.defaultName = rest.trim();
  }

  return result;
}

// Every distinct way this codebase declares an export (see this file's own
// header comment for why the list stops here instead of covering every
// legal ES export form).
const EXPORT_NAME_RES = [
  /^export\s+(?:async\s+)?function\s*\*?\s+([a-zA-Z0-9_$]+)/m,
  /^export\s+(?:const|let|var)\s+([a-zA-Z0-9_$]+)/m,
  /^export\s+class\s+([a-zA-Z0-9_$]+)/m,
];

function collectExports(source) {
  const named = new Set();
  let hasDefault = false;
  for (const line of stripComments(source).split('\n')) {
    if (/^export\s+default\b/.test(line)) {
      hasDefault = true;
      continue;
    }
    for (const re of EXPORT_NAME_RES) {
      const m = line.match(re);
      if (m) named.add(m[1]);
    }
  }
  return { named, hasDefault };
}

test('every named/default import in frontend/js resolves to a real export in its target module', () => {
  const frontendDir = resolveFrontendDir();
  const jsDir = path.join(frontendDir, 'js');
  const files = walkJsFiles(jsDir);

  const exportsByFile = new Map(); // absolute path -> { named: Set, hasDefault }
  const getExports = (absPath) => {
    if (!exportsByFile.has(absPath)) {
      let source;
      try {
        source = readFileSync(absPath, 'utf8');
      } catch {
        exportsByFile.set(absPath, null); // file doesn't exist at all
        return null;
      }
      exportsByFile.set(absPath, collectExports(source));
    }
    return exportsByFile.get(absPath);
  };

  const problems = [];

  for (const file of files) {
    const source = stripComments(readFileSync(file, 'utf8'));
    const relFile = path.relative(frontendDir, file);

    let match;
    IMPORT_RE.lastIndex = 0;
    while ((match = IMPORT_RE.exec(source)) !== null) {
      const [, clause, specifier] = match;

      // Only same-project relative imports are ours to check (this
      // codebase has no bare-specifier/npm imports in frontend/js at all,
      // besides the vendored ./vendor/qrcode.js, which IS relative).
      if (!specifier.startsWith('.')) continue;

      const targetAbs = path.resolve(path.dirname(file), specifier);
      const targetExports = getExports(targetAbs);
      const targetRel = path.relative(frontendDir, targetAbs);

      if (targetExports === null) {
        problems.push(`${relFile}: imports from "${specifier}" but ${targetRel} does not exist`);
        continue;
      }

      const { defaultName, namespaceName, named } = parseImportClause(clause);

      if (defaultName && !targetExports.hasDefault) {
        problems.push(`${relFile}: imports default "${defaultName}" from "${specifier}", but ${targetRel} has no "export default"`);
      }
      // namespaceName (`import * as x`) is always valid regardless of what
      // the target exports -- nothing to check.
      for (const { imported } of named) {
        if (!targetExports.named.has(imported)) {
          problems.push(`${relFile}: imports "{ ${imported} }" from "${specifier}", but ${targetRel} does not export a "${imported}"`);
        }
      }
    }
  }

  assert.deepEqual(
    problems,
    [],
    `broken import(s) found -- each of these is exactly the failure mode that kills EVERY page's JS at load time (see pages-load.test.mjs's header comment):\n  ${problems.join('\n  ')}`
  );
});
