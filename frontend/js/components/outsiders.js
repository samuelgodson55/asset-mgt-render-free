// =============================================================================
// js/components/outsiders.js
// -----------------------------------------------------------------------------
// Ad-Hoc (Unlinked) Directory table -- external individuals who've had
// assets dispatched to them without a full system user account. Custody
// Ledger for these rows is handled by components/custody.js (same modal
// used for Users, just with entityType='outsider').
// =============================================================================

import { apiRequest } from '../api.js';
import { escapeHtml, debounce, renderServerPaginationBar, openModal, closeModal, personAlertIcon, rowDetailsTrigger } from '../ui.js';

// TRUE server-side search + pagination (same pattern as components/
// audit.js's `auditState` / components/assets.js's `assetsState` /
// components/users.js's `usersState`): every keystroke in the search box
// (debounced), page turn, or "rows per page" change re-fetches just that
// slice from `GET /outsiders?search=&limit=&offset=` instead of
// re-filtering an already-downloaded array.
const outsidersState = { page: 1, perPage: 5, search: '', total: 0 };

// Keyed by id -> the full row object from the most recent
// renderOutsidersTable() call -- openEditOutsiderModal() reads straight
// from here to prefill the Edit form, same pattern as components/users.js's
// `usersById`.
let outsidersById = {};

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

  outsidersById = Object.fromEntries(outsiders.map(o => [o.id, o]));

  tbody.innerHTML = outsiders.map(o => {
    const initials = o.name.split(' ').map(p => p[0]).join('').slice(0, 2).toUpperCase();
    // Edit is available to both Super Admin/Admin and Manager -- ad-hoc
    // profiles aren't tied to a system-user role, so there's no narrower
    // boundary to enforce here (see services/outsider_service.py ->
    // update_outsider()).
    const actionButtons = `
      <button data-action="edit-outsider" data-outsider-id="${o.id}" class="rounded-md border border-border px-2.5 py-1.5 text-[12px] font-medium text-slate-300 transition hover:border-blue-500/50 hover:text-blue-400">Edit</button>
      <button data-action="open-custody" data-entity-id="${o.id}" data-entity-type="outsider" class="rounded-md border border-border px-2.5 py-1.5 text-[12px] font-medium text-slate-300 transition hover:border-blue-500/50 hover:text-blue-400">Custody Ledger</button>`;

    // Whole row is tappable on mobile -- see components/assets.js's
    // renderAssetsTable() for the full explanation of this pattern.
    return `
    <tr ${rowDetailsTrigger(escapeHtml(o.name), [
      ['Company', escapeHtml(o.company || '—')],
      ['Custody', `${o.outstanding_items} item${o.outstanding_items === 1 ? '' : 's'} checked out`],
      ['', `<div class="flex flex-wrap gap-2">${actionButtons}</div>`],
    ])} class="cursor-pointer transition hover:bg-card2/40 active:bg-card2/60 sm:cursor-default">
      <td class="px-5 py-3.5">
        <div class="flex items-center gap-3">
          <div class="flex h-8 w-8 items-center justify-center rounded-full bg-gradient-to-br from-amber-500 to-orange-600 text-[11px] font-bold text-white">${initials}</div>
          <div>
            <p class="flex items-center gap-1.5 font-medium text-slate-100">${escapeHtml(o.name)} ${personAlertIcon(o.alerts)}</p>
            <p class="tag-mono text-[11px] text-slate-500">${escapeHtml(o.contact_details)}</p>
          </div>
          <!-- Mobile-only affordance showing the row itself is tappable
               (replaces the old separate "Details" button). -->
          <svg class="ml-auto h-4 w-4 shrink-0 text-slate-600 sm:hidden" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 18l6-6-6-6"/></svg>
        </div>
      </td>
      <td class="hidden px-5 py-3.5 text-slate-300 sm:table-cell">${escapeHtml(o.company || '—')}</td>
      <td class="hidden px-5 py-3.5 tag-mono text-slate-300 sm:table-cell">${o.outstanding_items} item${o.outstanding_items === 1 ? '' : 's'} checked out</td>
      <td class="hidden px-5 py-3.5 sm:table-cell">
        <div class="flex flex-wrap justify-end gap-2">${actionButtons}</div>
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
  outsidersState.perPage = parseInt(value, 10) || 5;
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

// ---- Edit Ad-Hoc Individual (Super Admin/Admin and Manager) ----
let pendingEditOutsiderId = null;

function setEditOutsiderMessage(text, isError) {
  const msgEl = document.getElementById('editOutsiderMessage');
  if (!msgEl) return;
  msgEl.textContent = text || '';
  msgEl.classList.toggle('hidden', !text);
  msgEl.classList.toggle('text-rose-400', !!isError);
  msgEl.classList.toggle('text-emerald-400', !isError);
}

export function openEditOutsiderModal(outsiderId) {
  const o = outsidersById[outsiderId];
  if (!o) return;
  pendingEditOutsiderId = outsiderId;
  document.getElementById('editOutsiderTargetName').textContent = o.name;
  document.getElementById('editOutsiderName').value = o.name || '';
  document.getElementById('editOutsiderContact').value = o.contact_details || '';
  document.getElementById('editOutsiderCompany').value = o.company || '';
  setEditOutsiderMessage('', false);
  openModal('editOutsiderModal');
}

export async function submitEditOutsiderForm(event) {
  event.preventDefault();
  if (!pendingEditOutsiderId) return;
  setEditOutsiderMessage('', false);

  const payload = {
    name: document.getElementById('editOutsiderName').value,
    contact_details: document.getElementById('editOutsiderContact').value,
    company: document.getElementById('editOutsiderCompany').value,
  };

  try {
    await apiRequest(`/outsiders/${pendingEditOutsiderId}`, { method: 'PATCH', body: JSON.stringify(payload) });
    closeModal('editOutsiderModal');
    loadOutsiders();
  } catch (err) {
    setEditOutsiderMessage(err.message, true);
  }
}
