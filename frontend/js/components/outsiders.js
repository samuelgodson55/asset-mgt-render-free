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

    // Also populate every "Ad-Hoc Individual" dropdown that offers a
    // choice between an EXISTING unlinked profile and creating a new one
    // -- the Issue/Dispatch drawer's #adhocExistingSelect, the Quote
    // Detail screen's #quoteAssignAdhocExistingSelect, and the Create
    // Quote modal's #quoteAdhocExistingSelect. Same reasoning as
    // components/users.js's loadUsers() populating #staffSelect/
    // #customerSelect: a SEPARATE, unpaginated/unfiltered fetch (rather
    // than reusing `result.items` above) since this is a dropdown of
    // every valid existing profile, not the current page/search slice of
    // the Ad-Hoc Directory table.
    const adhocExistingSelect = document.getElementById('adhocExistingSelect');
    const quoteAssignAdhocExistingSelect = document.getElementById('quoteAssignAdhocExistingSelect');
    const quoteAdhocExistingSelect = document.getElementById('quoteAdhocExistingSelect');
    if (adhocExistingSelect || quoteAssignAdhocExistingSelect || quoteAdhocExistingSelect) {
      const roster = await apiRequest('/outsiders?limit=1000');
      const optionsHtml = '<option value="new">+ Create New Unlinked Profile</option>' + roster.items.map(o =>
        `<option value="${o.id}">${escapeHtml(o.name)}${o.company ? ` (${escapeHtml(o.company)})` : ''}</option>`
      ).join('');
      if (adhocExistingSelect) adhocExistingSelect.innerHTML = optionsHtml;
      if (quoteAssignAdhocExistingSelect) quoteAssignAdhocExistingSelect.innerHTML = optionsHtml;
      if (quoteAdhocExistingSelect) quoteAdhocExistingSelect.innerHTML = optionsHtml;
    }
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
      <button data-action="open-custody" data-entity-id="${o.id}" data-entity-type="outsider" class="rounded-md border border-border px-2.5 py-1.5 text-[12px] font-medium text-slate-300 transition hover:border-blue-500/50 hover:text-blue-400">Custody Ledger</button>
      <button data-action="convert-outsider" data-outsider-id="${o.id}" class="rounded-md border border-border px-2.5 py-1.5 text-[12px] font-medium text-slate-300 transition hover:border-emerald-500/50 hover:text-emerald-400">Convert to User</button>
      <button data-action="delete-outsider" data-outsider-id="${o.id}" data-outsider-name="${escapeHtml(o.name)}" class="rounded-md border border-border px-2.5 py-1.5 text-[12px] font-medium text-rose-400 transition hover:border-rose-500/50 hover:text-rose-300">Delete</button>`;

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
            <p class="tag-mono text-[11px] text-slate-500">${[o.email, o.phone_number].filter(Boolean).map(escapeHtml).join(' · ') || '—'}</p>
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

// ---- Delete Ad-Hoc Individual (Super Admin/Admin and Manager) ----
// Mirrors components/users.js's deleteProfile() / components/backups.js's
// deleteBackup(): confirm, DELETE, then refresh the table. Backend soft-
// deletes the row (see services/outsider_service.py -> delete_outsider())
// and blocks the request with a 400 if the profile still has items in
// active custody -- apiRequest()'s thrown Error already carries that
// message straight from the backend's `detail`, so the alert() below
// surfaces it verbatim.
export async function deleteOutsider(outsiderId, outsiderName) {
  if (!confirm(`Delete the Ad-Hoc profile for ${outsiderName}? This cannot be undone.`)) return;
  try {
    await apiRequest(`/outsiders/${outsiderId}`, { method: 'DELETE' });
    loadOutsiders();
  } catch (err) {
    alert(`Delete failed: ${err.message}`);
  }
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
  document.getElementById('editOutsiderEmail').value = o.email || '';
  const phoneInput = document.getElementById('editOutsiderPhone');
  if (phoneInput) phoneInput.value = o.phone_number || '';
  document.getElementById('editOutsiderCompany').value = o.company || '';
  setEditOutsiderMessage('', false);
  openModal('editOutsiderModal');
}

export async function submitEditOutsiderForm(event) {
  event.preventDefault();
  if (!pendingEditOutsiderId) return;
  setEditOutsiderMessage('', false);

  const phoneInput = document.getElementById('editOutsiderPhone');
  const payload = {
    name: document.getElementById('editOutsiderName').value,
    email: document.getElementById('editOutsiderEmail').value || null,
    phone_number: phoneInput ? (phoneInput.value || null) : null,
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

// ---- Convert to Real User Account (Super Admin/Admin and Manager) --------
// "The outsider finally decides he wants a login": POST
// /outsiders/{id}/convert-to-user (see services/outsider_service.py ->
// convert_outsider_to_user()). The ROLE dropdown available here is
// restricted per-page exactly like createUserForm's #newUserRole
// (admin.html offers Admin/Manager/Staff/Customer, manager.html offers
// only Staff/Customer) -- the backend independently re-enforces that same
// ceiling, so a Manager can't grant elevated access even by tampering
// with the page or calling the API directly.
let pendingConvertOutsiderId = null;

function setConvertOutsiderMessage(text, isError) {
  const msgEl = document.getElementById('convertOutsiderMessage');
  if (!msgEl) return;
  msgEl.textContent = text || '';
  msgEl.classList.toggle('hidden', !text);
  msgEl.classList.toggle('text-rose-400', !!isError);
  msgEl.classList.toggle('text-emerald-400', !isError);
}

export function openConvertOutsiderModal(outsiderId) {
  const o = outsidersById[outsiderId];
  if (!o) return;
  pendingConvertOutsiderId = outsiderId;
  document.getElementById('convertOutsiderTargetName').textContent = o.name;
  // Prefills from this profile's own on-file email/phone -- still fully
  // editable, just a convenience starting point (e.g. someone might want
  // to log in with a different address than the one clients reach them
  // at). No more guessing required now that email/phone_number are their
  // own real fields instead of one ambiguous free-text contact_details.
  document.getElementById('convertOutsiderEmail').value = o.email || '';
  const phoneInput = document.getElementById('convertOutsiderPhone');
  if (phoneInput) phoneInput.value = o.phone_number || '';
  document.getElementById('convertOutsiderRole').value = 'staff';
  document.getElementById('convertOutsiderPassword').value = '';
  document.getElementById('convertOutsiderDepartment').value = '';
  document.getElementById('convertOutsiderDeptRole').value = '';
  setConvertOutsiderMessage('', false);
  openModal('convertOutsiderModal');
}

export async function submitConvertOutsiderForm(event) {
  event.preventDefault();
  if (!pendingConvertOutsiderId) return;
  setConvertOutsiderMessage('', false);

  const passwordInput = document.getElementById('convertOutsiderPassword');
  const phoneInput = document.getElementById('convertOutsiderPhone');
  const payload = {
    email: document.getElementById('convertOutsiderEmail').value,
    phone_number: phoneInput ? (phoneInput.value || null) : null,
    role: document.getElementById('convertOutsiderRole').value,
    password: passwordInput.value,
    department: document.getElementById('convertOutsiderDepartment').value || null,
    department_role: document.getElementById('convertOutsiderDeptRole').value || null,
  };

  try {
    const result = await apiRequest(`/outsiders/${pendingConvertOutsiderId}/convert-to-user`, {
      method: 'POST', body: JSON.stringify(payload),
    });
    closeModal('convertOutsiderModal');
    // Same "don't leave a plaintext password sitting in the DOM" fix as
    // submitCreateUserForm() in components/users.js.
    if (passwordInput) passwordInput.value = '';
    alert(result.message);
    loadOutsiders();
    pendingConvertOutsiderId = null;
  } catch (err) {
    setConvertOutsiderMessage(err.message, true);
    if (passwordInput) passwordInput.value = '';
  }
}
