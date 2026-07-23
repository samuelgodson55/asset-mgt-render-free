// =============================================================================
// frontend/tests/helpers/strip-comments.mjs
// -----------------------------------------------------------------------------
// This codebase's own JS files are heavily commented, frequently quoting
// real-looking snippets of import/export syntax in prose (e.g.
// dashboard.js's header: "components/*.js can `import { refreshDashboard }
// from './dashboard.js'`"). A naive regex scan for import/export statements
// over raw source text matches those comment snippets too, producing bogus
// results. This strips // and /* */ comments (replacing their contents with
// spaces, so line/column positions of everything else are preserved) while
// leaving '...'/"..."/`...` string and template literal contents untouched,
// so a real import specifier or export name is never accidentally erased.
// =============================================================================

export function stripComments(source) {
  let out = '';
  let i = 0;
  const n = source.length;
  while (i < n) {
    const c = source[i];
    const next = source[i + 1];

    if (c === '/' && next === '/') {
      // Line comment: blank out everything to (but not including) the
      // newline, so line numbers stay aligned.
      while (i < n && source[i] !== '\n') {
        out += ' ';
        i++;
      }
      continue;
    }

    if (c === '/' && next === '*') {
      out += '  ';
      i += 2;
      while (i < n && !(source[i] === '*' && source[i + 1] === '/')) {
        out += source[i] === '\n' ? '\n' : ' ';
        i++;
      }
      out += '  ';
      i += 2;
      continue;
    }

    if (c === '"' || c === "'" || c === '`') {
      const quote = c;
      out += c;
      i++;
      while (i < n && source[i] !== quote) {
        if (source[i] === '\\' && i + 1 < n) {
          out += source[i] + source[i + 1];
          i += 2;
          continue;
        }
        out += source[i];
        i++;
      }
      if (i < n) {
        out += source[i]; // closing quote
        i++;
      }
      continue;
    }

    out += c;
    i++;
  }
  return out;
}
