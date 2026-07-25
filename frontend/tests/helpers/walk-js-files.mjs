// =============================================================================
// frontend/tests/helpers/walk-js-files.mjs
// -----------------------------------------------------------------------------
// Recursively lists every .js file under a directory (used to sweep all of
// frontend/js/**, including js/components/). Skips node_modules defensively
// even though none of this test's directories should ever contain one.
// =============================================================================

import { readdirSync } from 'node:fs';
import path from 'node:path';

export function walkJsFiles(dir) {
  let results = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === 'node_modules') continue;
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      results = results.concat(walkJsFiles(full));
    } else if (entry.name.endsWith('.js')) {
      results.push(full);
    }
  }
  return results;
}
