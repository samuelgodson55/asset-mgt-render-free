// =============================================================================
// js/components/outsiders.js
// -----------------------------------------------------------------------------
// Ad-Hoc (Unlinked) Directory table -- external individuals who've had
// assets dispatched to them without a full system user account. Custody
// Ledger for these rows is handled by components/custody.js (same modal
// used for Users, just with entityType='outsider').
// =============================================================================

import { apiRequest } from '../api.js';
import { escapeHtml, debounce, renderServerPaginationBar } from '../ui.js';

// TRUE server-side search + pagination (same pattern as components/
// audit.js's `auditState` / components/assets.js's `assetsState` /
// components/users.js's `usersState`): every keystroke in the search box
// (debounced), page turn, or "rows per page" change re-fetches just that
// slice from `GET /outsiders?search=&limit=&offset=` instead of
// re-filtering an already-downloaded array.
const outsidersState = { page: 1, perPage: 10, search: '', total: 0 };

export async function loadOutsiders() {
  const tbody = document.getElementById('outsiderTableBody');
  if (!tbody) return; // this page doesn't have an ad-hoc directory table
  try {
    const offset = (outsidersState.page - 1) * outsidersState.perPage;
    const params = new URLSearchParams({ limit: outsidersState.perPage, offset });
    if (outsidersState.search.trim()) params.set('search', outsidersState.search.trim());
    const result = await apiRequest(`/outsiders?${params.toString()}`);
    outsidersState.total = result.total;
    renderOutsidersTable(result.items);
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="4" class="px-5 py-6 text-center text-rose-400">${escapeHtml(err.message)}</td></tr>`;
  }
}

function renderOutsidersTable(outsiders) {
  const tbody = document.getElementById('outsiderTableBody');
  if (!tbody) return;

  document.querySelectorAll('.outsider-count').forEach(el => el.textContent = outsidersState.total);

  tbody.innerHTML = outsiders.map(o => {
    const initials = o.name.split(' ').map(p => p[0]).join('').slice(0, 2).toUpperCase();
    return `
    <tr class="transition hover:bg-card2/40">
      <td class="px-5 py-3.5">
        <div class="flex items-center gap-3">
          <div class="flex h-8 w-8 items-center justify-center rounded-full bg-gradient-to-br from-amber-500 to-orange-600 text-[11px] font-bold text-white">${initials}</div>
          <div>
            <p class="font-medium text-slate-100">${escapeHtml(o.name)}</p>
            <p class="tag-mono text-[11px] text-slate-500">${escapeHtml(o.contact_details)}</p>
          </div>
        </div>
      </td>
      <td class="px-5 py-3.5 text-slate-300">${escapeHtml(o.company || '—')}</td>
      <td class="px-5 py-3.5 tag-mono text-slate-300">${o.outstanding_items} item${o.outstanding_items === 1 ? '' : 's'} checked out</td>
      <td class="px-5 py-3.5">
        <div class="flex justify-end gap-2">
          <button data-action="open-custody" data-entity-id="${o.id}" data-entity-type="outsider" class="rounded-md border border-border px-2.5 py-1.5 text-[12px] font-medium text-slate-300 transition hover:border-blue-500/50 hover:text-blue-400">Custody Ledger</button>
        </div>
      </td>
    </tr>`;
  }).join('') || `<tr><td colspan="4" class="px-5 py-6 text-center text-slate-500">No ad-hoc individuals on file yet.</td></tr>`;

  renderServerPaginationBar('outsiders', outsidersState);
}

// Called from the search box's 'input' listener (main.js), debounced.
export const setOutsidersSearch = debounce((value) => {
  outsidersState.search = value;
  outsidersState.page = 1; // always jump back to page 1 on a new search
  loadOutsiders();
});

// Called from the "Rows per page" <select>'s 'change' listener (main.js).
export function setOutsidersPerPage(value) {
  outsidersState.perPage = parseInt(value, 10) || 10;
  outsidersState.page = 1;
  loadOutsiders();
}

// Called by main.js's delegated click handler when Prev/Next is clicked.
export function changeOutsidersPage(delta) {
  const nextPage = outsidersState.page + delta;
  if (nextPage < 1) return;
  outsidersState.page = nextPage;
  loadOutsiders();
}
