// =============================================================================
// js/components/users.js
// -----------------------------------------------------------------------------
// "User Directory" / "Team Allocation Matrix" table, Delete Profile action,
// Reset Password action, the Restore Deleted Users panel, and the Provision
// System User Account form.
// =============================================================================

import { apiRequest } from '../api.js';
import { getSession } from '../auth.js';
import { escapeHtml, debounce, renderServerPaginationBar, openModal, closeModal, personAlertIcon, formatTimestamp, rowDetailsTrigger } from '../ui.js';
import { refreshDashboard } from '../dashboard.js';

// TRUE server-side search + pagination (same pattern as components/
// audit.js's `auditState` / components/assets.js's `assetsState`): every
// keystroke in the search box (debounced), page turn, or "rows per page"
// change re-fetches just that slice from `GET /users?search=&limit=&
// offset=` instead of re-filtering an already-downloaded array.
const usersState = { page: 1, perPage: 5, search: '', total: 0 };

// Keyed by id -> the full row object from the most recent renderUsersTable()
// call. openEditUserModal() reads straight from here to prefill the Edit
// form instead of firing off a separate GET -- the User Directory table
// already has every field the edit form needs (name/username/email) sitting
// right there on each row.
let usersById = {};

// ---- User Directory / Team Allocation Matrix table ----
export async function loadUsers() {
  const tbody = document.getElementById('userTableBody');
  if (!tbody) return;
  try {
    const offset = (usersState.page - 1) * usersState.perPage;
    const params = new URLSearchParams({ limit: usersState.perPage, offset });
    if (usersState.search.trim()) params.set('search', usersState.search.trim());
    const result = await apiRequest(`/users?${params.toString()}`);
    usersState.total = result.total;
    renderUsersTable(result.items);

    // Also populate the "Assign To > Staff Member" dropdown in the dispatch
    // drawer. This is a SEPARATE, unpaginated/unfiltered fetch (rather than
    // reusing `result.items` above) because it's a dropdown of every valid
    // dispatch recipient, not a table to page/search through -- reusing
    // the current page's search-narrowed slice would hide valid recipients
    // the moment a Super Admin/Manager typed anything into the User
    // Directory's search box.
    const staffSelect = document.getElementById('staffSelect');
    const customerSelect = document.getElementById('customerSelect');
    // Create Quote modal's own Staff/Customer selects -- separate element
    // ids from the dispatch drawer's above since both modals can exist on
    // the same admin.html/manager.html page at once, but populated from
    // the exact same roster fetch/filtering (see components/quotation.js's
    // openCreateQuoteModal()).
    const quoteStaffSelect = document.getElementById('quoteStaffSelect');
    const quoteCustomerSelect = document.getElementById('quoteCustomerSelect');
    if (staffSelect || customerSelect || quoteStaffSelect || quoteCustomerSelect) {
      const roster = await apiRequest('/users?limit=1000');
      if (staffSelect) {
        // The "Staff Member" route is for internal personnel only --
        // role="customer" accounts belong on the separate "Linked Customer
        // Account" route (customerSelect below), not here.
        const staff = roster.items.filter(u => u.role !== 'customer');
        staffSelect.innerHTML = staff.length
          ? staff.map(u => `<option value="${u.id}">${escapeHtml(u.name)} (${escapeHtml(u.department_role || u.role)})</option>`).join('')
          : `<option value="" disabled selected>No staff accounts on file</option>`;
      }
      if (customerSelect) {
        // The "Linked Customer Account" route only ever dispatches to a
        // real, login-capable role="customer" User (see
        // services/outsider_service.py's module docstring for how that
        // differs from an "Ad-Hoc Individual" -- a models.Outsider row
        // with no login). So this dropdown is the same roster fetch as
        // staffSelect above, just narrowed to customer accounts, rather
        // than a free-text field that used to fabricate a brand new
        // Outsider record on every dispatch.
        const customers = roster.items.filter(u => u.role === 'customer');
        customerSelect.innerHTML = customers.length
          ? customers.map(u => `<option value="${u.id}">${escapeHtml(u.name)} (${escapeHtml(u.email)})</option>`).join('')
          : `<option value="" disabled selected>No linked customer accounts on file</option>`;
      }
      if (quoteStaffSelect) {
        const staff = roster.items.filter(u => u.role !== 'customer');
        quoteStaffSelect.innerHTML = staff.length
          ? staff.map(u => `<option value="${u.id}">${escapeHtml(u.name)} (${escapeHtml(u.department_role || u.role)})</option>`).join('')
          : `<option value="" disabled selected>No staff accounts on file</option>`;
      }
      if (quoteCustomerSelect) {
        const customers = roster.items.filter(u => u.role === 'customer');
        quoteCustomerSelect.innerHTML = customers.length
          ? customers.map(u => `<option value="${u.id}">${escapeHtml(u.name)} (${escapeHtml(u.email)})</option>`).join('')
          : `<option value="" disabled selected>No linked customer accounts on file</option>`;
      }
    }
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="4" class="px-5 py-6 text-center text-rose-400">${escapeHtml(err.message)}</td></tr>`;
  }
}

function renderUsersTable(users) {
  const tbody = document.getElementById('userTableBody');
  if (!tbody) return;
  const isManagerView = document.body.dataset.view === 'manager';

  document.querySelectorAll('.user-count').forEach(el => el.textContent = usersState.total);

  usersById = Object.fromEntries(users.map(u => [u.id, u]));

  tbody.innerHTML = users.map(u => {
    const initials = u.name.split(' ').map(p => p[0]).join('').slice(0, 2).toUpperCase();
    // `checkout_count` comes straight from the backend (GET /users), which
    // sums up the outstanding quantity across that user's active checkouts.
    const custodyLabel = `${u.checkout_count ?? 0} item${(u.checkout_count ?? 0) === 1 ? '' : 's'} checked out`;
    // Edit is available to a Super Admin/Admin for every account, but a
    // Manager only ever sees it on "staff"/"customer" rows -- mirrors the
    // exact same MANAGER_PROVISIONABLE_ROLES boundary the backend enforces
    // in services/user_service.py -> update_user(), so a Manager is never
    // shown a button that would just come back as a 403.
    const canEdit = !isManagerView || u.role === 'staff' || u.role === 'customer';
    const actionButtons = `
      ${canEdit ? `<button data-action="edit-user" data-user-id="${u.id}" class="rounded-md border border-border px-2.5 py-1.5 text-[12px] font-medium text-slate-300 transition hover:border-blue-500/50 hover:text-blue-400">Edit</button>` : ''}
      <button data-action="open-custody" data-entity-id="${u.id}" data-entity-type="user" class="rounded-md border border-border px-2.5 py-1.5 text-[12px] font-medium text-slate-300 transition hover:border-blue-500/50 hover:text-blue-400">Custody Ledger</button>
      ${isManagerView ? '' : `<button data-action="reset-password" data-user-id="${u.id}" data-user-name="${escapeHtml(u.name)}" class="rounded-md border border-border px-2.5 py-1.5 text-[12px] font-medium text-slate-300 transition hover:border-amber-500/50 hover:text-amber-400">Reset Password</button>`}
      ${canEdit ? `<button data-action="revoke-user" data-user-id="${u.id}" data-user-name="${escapeHtml(u.name)}" class="rounded-md border border-amber-500/30 px-2.5 py-1.5 text-[12px] font-medium text-amber-400 transition hover:border-amber-500 hover:bg-amber-500/10">Revoke Access</button>` : ''}
      ${isManagerView ? '' : `<button data-action="delete-profile" data-user-id="${u.id}" data-user-name="${escapeHtml(u.name)}" class="rounded-md border border-rose-500/30 px-2.5 py-1.5 text-[12px] font-medium text-rose-400 transition hover:border-rose-500 hover:bg-rose-500/10">Delete Profile</button>`}`;

    // Whole row is tappable on mobile -- see components/assets.js's
    // renderAssetsTable() for the full explanation of this pattern.
    return `
    <tr ${rowDetailsTrigger(escapeHtml(u.name), [
      [isManagerView ? 'Department Role' : 'Privilege Tier', escapeHtml(isManagerView ? (u.department_role || 'Team Member') : u.role.replace('_', ' '))],
      ['Custody', custodyLabel],
      ['', `<div class="flex flex-wrap gap-2">${actionButtons}</div>`],
    ])} class="cursor-pointer transition hover:bg-card2/40 active:bg-card2/60 sm:cursor-default">
      <td class="px-5 py-3.5">
        <div class="flex items-center gap-3">
          <div class="flex h-8 w-8 items-center justify-center rounded-full bg-gradient-to-br from-blue-500 to-indigo-600 text-[11px] font-bold text-white">${initials}</div>
          <div>
            <p class="flex items-center gap-1.5 font-medium text-slate-100">${escapeHtml(u.name)} ${personAlertIcon(u.alerts)}</p>
            <p class="tag-mono text-[11px] text-slate-500">${escapeHtml(u.email)}</p>
          </div>
          <!-- Mobile-only affordance showing the row itself is tappable
               (replaces the old separate "Details" button). -->
          <svg class="ml-auto h-4 w-4 shrink-0 text-slate-600 sm:hidden" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 18l6-6-6-6"/></svg>
        </div>
      </td>
      <td class="hidden px-5 py-3.5 sm:table-cell">
        <span class="inline-flex items-center gap-1 rounded-full bg-blue-500/10 px-2.5 py-1 text-[11px] font-semibold text-blue-400 ring-1 ring-blue-500/30">
          <span class="h-1.5 w-1.5 rounded-full bg-blue-500"></span> ${escapeHtml(isManagerView ? (u.department_role || 'Team Member') : u.role.replace('_', ' '))}
        </span>
      </td>
      <td class="hidden px-5 py-3.5 tag-mono text-slate-300 sm:table-cell">${custodyLabel}</td>
      <td class="hidden px-5 py-3.5 sm:table-cell">
        <div class="flex flex-wrap justify-end gap-2">${actionButtons}</div>
      </td>
    </tr>`;
  }).join('') || `<tr><td colspan="4" class="px-5 py-6 text-center text-slate-500">No accounts found.</td></tr>`;

  renderServerPaginationBar('users', usersState);
}

// Called from the search box's 'input' listener (main.js), debounced.
export const setUsersSearch = debounce((value) => {
  usersState.search = value;
  usersState.page = 1; // always jump back to page 1 on a new search
  loadUsers();
});

// Called from the "Rows per page" <select>'s 'change' listener (main.js).
export function setUsersPerPage(value) {
  usersState.perPage = parseInt(value, 10) || 5;
  usersState.page = 1;
  loadUsers();
}

// Called by main.js's delegated click handler when Prev/Next is clicked.
export function changeUsersPage(delta) {
  const nextPage = usersState.page + delta;
  if (nextPage < 1) return;
  usersState.page = nextPage;
  loadUsers();
}

// ---- Edit User Details (Super Admin/Admin: all accounts; Manager: Staff/Customer only) ----
// Same "remember which row the open modal is acting on" pattern as
// pendingResetPasswordUserId below.
let pendingEditUserId = null;

function setEditUserMessage(text, isError) {
  const msgEl = document.getElementById('editUserMessage');
  if (!msgEl) return;
  msgEl.textContent = text || '';
  msgEl.classList.toggle('hidden', !text);
  msgEl.classList.toggle('text-rose-400', !!isError);
  msgEl.classList.toggle('text-emerald-400', !isError);
}

export function openEditUserModal(userId) {
  const u = usersById[userId];
  if (!u) return;
  pendingEditUserId = userId;
  document.getElementById('editUserTargetName').textContent = u.name;
  document.getElementById('editUserName').value = u.name || '';
  document.getElementById('editUserUsername').value = u.username || '';
  document.getElementById('editUserEmail').value = u.email || '';
  setEditUserMessage('', false);
  openModal('editUserModal');
}

export async function submitEditUserForm(event) {
  event.preventDefault();
  if (!pendingEditUserId) return;
  setEditUserMessage('', false);

  const payload = {
    name: document.getElementById('editUserName').value,
    username: document.getElementById('editUserUsername').value,
    email: document.getElementById('editUserEmail').value,
  };

  try {
    await apiRequest(`/users/${pendingEditUserId}`, { method: 'PATCH', body: JSON.stringify(payload) });
    closeModal('editUserModal');
    loadUsers();
    refreshDashboard();
  } catch (err) {
    setEditUserMessage(err.message, true);
  }
}

// ---- Delete Profile (Super Admin only) ----
export async function deleteProfile(userId, userName) {
  // Requirement #4 frontend safeguard: intercept and block a Super Admin
  // trying to delete their own active session's account BEFORE the
  // request even goes out. The backend enforces this too (defense in
  // depth), but catching it here gives an immediate, clear message
  // instead of a round-trip 403.
  const session = getSession();
  if (session && String(session.sub) === String(userId)) {
    alert("You cannot delete your own account while logged in as it.");
    return;
  }
  if (!confirm(`Delete profile for ${userName}? It will be removed from active users, but can be restored later from the Restore Deleted Users panel. Only Users with no outstanding checkouts can be deleted..`)) return;
  try {
    await apiRequest(`/users/${userId}`, { method: 'DELETE' });
    refreshDashboard();
  } catch (err) {
    alert(err.message);
  }
}

// ---- Revoke Login Access (Super Admin/Admin and Manager -- "the reverse
// of Outsider -> User", see components/outsiders.js's
// openConvertOutsiderModal()/submitConvertOutsiderForm() and
// services/user_service.py -> convert_user_to_outsider()) ----
let pendingRevokeUserId = null;

function setRevokeUserMessage(text, isError) {
  const msgEl = document.getElementById('revokeUserMessage');
  if (!msgEl) return;
  msgEl.textContent = text || '';
  msgEl.classList.toggle('hidden', !text);
  msgEl.classList.toggle('text-rose-400', !!isError);
  msgEl.classList.toggle('text-emerald-400', !isError);
}

export function openRevokeUserModal(userId, userName) {
  const u = usersById[userId];
  pendingRevokeUserId = userId;
  document.getElementById('revokeUserTargetName').textContent = userName || (u && u.name) || '';
  document.getElementById('revokeUserContact').value = (u && u.email) || '';
  document.getElementById('revokeUserCompany').value = '';
  setRevokeUserMessage('', false);
  openModal('revokeUserModal');
}

export async function submitRevokeUserForm(event) {
  event.preventDefault();
  if (!pendingRevokeUserId) return;
  setRevokeUserMessage('', false);

  const payload = {
    contact_details: document.getElementById('revokeUserContact').value || null,
    company: document.getElementById('revokeUserCompany').value || null,
  };

  try {
    const result = await apiRequest(`/users/${pendingRevokeUserId}/convert-to-outsider`, {
      method: 'POST', body: JSON.stringify(payload),
    });
    closeModal('revokeUserModal');
    alert(result.message);
    loadUsers();
    refreshDashboard();
    pendingRevokeUserId = null;
  } catch (err) {
    setRevokeUserMessage(err.message, true);
  }
}

// ---- Reset Password (Super Admin/Admin only -- "forgot password" recovery) ----
// Remembers which account the currently-open modal is acting on, exactly
// like components/extensions.js's `pendingDirectExtendCheckoutId` does for
// the Extend Due Date modal -- the modal itself has no user-id field of
// its own, so the submit handler needs somewhere to read it back from.
let pendingResetPasswordUserId = null;

function setResetPasswordMessage(text, isError) {
  const msgEl = document.getElementById('resetPasswordMessage');
  if (!msgEl) return;
  msgEl.textContent = text || '';
  msgEl.classList.toggle('hidden', !text);
  msgEl.classList.toggle('text-rose-400', !!isError);
  msgEl.classList.toggle('text-emerald-400', !isError);
}

export function openResetPasswordModal(userId, userName) {
  pendingResetPasswordUserId = userId;
  document.getElementById('resetPasswordUserName').textContent = userName;
  document.getElementById('resetPasswordForm').reset();
  setResetPasswordMessage('', false);
  openModal('resetPasswordModal');
}

export async function submitResetPasswordForm(event) {
  event.preventDefault();
  if (!pendingResetPasswordUserId) return;
  setResetPasswordMessage('', false);

  const newPassword = document.getElementById('resetPasswordNew').value;
  const confirmPassword = document.getElementById('resetPasswordConfirm').value;
  const adminPassword = document.getElementById('resetPasswordAdminConfirm').value;

  // Client-side check purely for immediate feedback -- the backend
  // independently re-validates password strength no matter what the
  // browser already checked (schemas/users.py's UserPasswordResetRequest).
  if (newPassword !== confirmPassword) {
    setResetPasswordMessage('New password and confirmation do not match.', true);
    return;
  }

  // Same "immediate feedback, backend is the real gate" idea as the
  // strength check above -- the actual re-auth is enforced server-side in
  // reset_user_password(), this just avoids a round trip for the obvious
  // empty-field case.
  if (!adminPassword) {
    setResetPasswordMessage('Enter your own password to confirm this reset.', true);
    return;
  }

  try {
    const result = await apiRequest(`/users/${pendingResetPasswordUserId}/reset-password`, {
      method: 'POST',
      body: JSON.stringify({ new_password: newPassword, admin_password: adminPassword }),
    });
    setResetPasswordMessage(result.message || 'Password reset successfully.', false);
    document.getElementById('resetPasswordForm').reset();
  } catch (err) {
    setResetPasswordMessage(err.message, true);
  }
}

// ---- Restore Deleted Users (Super Admin/Admin only) ----
// Same true server-side search + pagination pattern as usersState above,
// against its own separate GET /users/deleted list.
const deletedUsersState = { page: 1, perPage: 5, search: '', total: 0 };

export async function loadDeletedUsers() {
  const tbody = document.getElementById('deletedUserTableBody');
  if (!tbody) return; // Not on this page (e.g. manager.html has no restore panel).
  try {
    const offset = (deletedUsersState.page - 1) * deletedUsersState.perPage;
    const params = new URLSearchParams({ limit: deletedUsersState.perPage, offset });
    if (deletedUsersState.search.trim()) params.set('search', deletedUsersState.search.trim());
    const result = await apiRequest(`/users/deleted?${params.toString()}`);
    deletedUsersState.total = result.total;
    renderDeletedUsersTable(result.items);
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="4" class="px-5 py-6 text-center text-rose-400">${escapeHtml(err.message)}</td></tr>`;
  }
}

function renderDeletedUsersTable(users) {
  const tbody = document.getElementById('deletedUserTableBody');
  if (!tbody) return;

  document.querySelectorAll('.deleted-user-count').forEach(el => el.textContent = deletedUsersState.total);

  tbody.innerHTML = users.map(u => {
    const initials = u.name.split(' ').map(p => p[0]).join('').slice(0, 2).toUpperCase();
    const actionButtons = `<button data-action="restore-user" data-user-id="${u.id}" data-user-name="${escapeHtml(u.name)}" class="rounded-md border border-emerald-500/30 px-2.5 py-1.5 text-[12px] font-medium text-emerald-400 transition hover:border-emerald-500 hover:bg-emerald-500/10">Restore</button>
      <button data-action="purge-user" data-user-id="${u.id}" data-user-name="${escapeHtml(u.name)}" class="rounded-md border border-rose-500/30 px-2.5 py-1.5 text-[12px] font-medium text-rose-400 transition hover:border-rose-500 hover:bg-rose-500/10">Purge</button>`;

    return `
    <tr ${rowDetailsTrigger(escapeHtml(u.name), [
      ['Privilege Tier', escapeHtml(u.role.replace('_', ' '))],
      ['Deleted On', u.deleted_at ? escapeHtml(formatTimestamp(u.deleted_at)) : '—'],
      ['', `<div class="flex flex-wrap gap-2">${actionButtons}</div>`],
    ])} class="cursor-pointer transition hover:bg-card2/40 active:bg-card2/60 sm:cursor-default">
      <td class="px-5 py-3.5">
        <div class="flex items-center gap-3">
          <div class="flex h-8 w-8 items-center justify-center rounded-full bg-gradient-to-br from-slate-500 to-slate-600 text-[11px] font-bold text-white">${initials}</div>
          <div>
            <p class="font-medium text-slate-100">${escapeHtml(u.name)}</p>
            <p class="tag-mono text-[11px] text-slate-500">${escapeHtml(u.email)}</p>
          </div>
          <!-- Mobile-only affordance showing the row itself is tappable
               (replaces the old separate "Details" button). -->
          <svg class="ml-auto h-4 w-4 shrink-0 text-slate-600 sm:hidden" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 18l6-6-6-6"/></svg>
        </div>
      </td>
      <td class="hidden px-5 py-3.5 sm:table-cell">
        <span class="inline-flex items-center gap-1 rounded-full bg-slate-500/10 px-2.5 py-1 text-[11px] font-semibold text-slate-400 ring-1 ring-slate-500/30">
          <span class="h-1.5 w-1.5 rounded-full bg-slate-500"></span> ${escapeHtml(u.role.replace('_', ' '))}
        </span>
      </td>
      <td class="hidden px-5 py-3.5 tag-mono text-slate-400 sm:table-cell" title="${escapeHtml(u.deleted_at || '')}">${u.deleted_at ? formatTimestamp(u.deleted_at) : '—'}</td>
      <td class="hidden px-5 py-3.5 sm:table-cell">
        <div class="flex flex-wrap justify-end gap-2">${actionButtons}</div>
      </td>
    </tr>`;
  }).join('') || `<tr><td colspan="4" class="px-5 py-6 text-center text-slate-500">No deleted accounts.</td></tr>`;

  renderServerPaginationBar('deletedUsers', deletedUsersState);
}

export const setDeletedUsersSearch = debounce((value) => {
  deletedUsersState.search = value;
  deletedUsersState.page = 1;
  loadDeletedUsers();
});

export function setDeletedUsersPerPage(value) {
  deletedUsersState.perPage = parseInt(value, 10) || 5;
  deletedUsersState.page = 1;
  loadDeletedUsers();
}

export function changeDeletedUsersPage(delta) {
  const nextPage = deletedUsersState.page + delta;
  if (nextPage < 1) return;
  deletedUsersState.page = nextPage;
  loadDeletedUsers();
}

export async function restoreUser(userId, userName) {
  if (!confirm(`Restore ${userName}'s account? They will be able to log in again immediately.`)) return;
  try {
    await apiRequest(`/users/${userId}/restore`, { method: 'POST' });
    loadDeletedUsers();
    refreshDashboard();
  } catch (err) {
    alert(err.message);
  }
}

// Purge is deliberately a separate, more strongly-worded confirmation than
// Restore: it's irreversible (no "unpurge") and its whole point is to erase
// the account's email/username so a new account can reuse them -- so the
// warning says exactly that, rather than reusing restoreUser()'s wording.
export async function purgeUser(userId, userName) {
  if (!confirm(
    `Permanently purge ${userName}'s deleted account? This cannot be undone. `
    + `Their email and username will be freed up for reuse by a new account, `
    + `but this account can no longer be restored afterward.`
  )) return;
  try {
    await apiRequest(`/users/${userId}/purge`, { method: 'POST' });
    loadDeletedUsers();
    refreshDashboard();
  } catch (err) {
    alert(err.message);
  }
}

// ---- Provision System User Account (Super Admin + Manager) ----
// Managers reach this same form/handler (see admin.html/manager.html); the
// ROLE dropdown available to them is limited to Staff/Customer directly in
// the HTML, and the backend independently re-checks + enforces that same
// limit in POST /users, so a manager can't grant themselves admin rights
// even by tampering with the page or calling the API directly.
export async function submitCreateUserForm(event) {
  event.preventDefault();
  const passwordInput = document.getElementById('newUserPassword');
  const payload = {
    name: document.getElementById('newUserName').value,
    email: document.getElementById('newUserEmail').value,
    role: document.getElementById('newUserRole').value,
    password: passwordInput.value,
    department: document.getElementById('newUserDepartment').value || null,
    department_role: document.getElementById('newUserDeptRole').value || null,
  };
  try {
    const result = await apiRequest('/users', { method: 'POST', body: JSON.stringify(payload) });
    alert(result.message);
    document.getElementById('createUserForm').reset();
    // Security fix: explicitly blank the password field's in-memory value
    // right after submitting, on top of the form .reset() above. .reset()
    // normally clears it already, but some browsers can restore an
    // autofilled value into a "reset" field -- this line guarantees the
    // plaintext password isn't left sitting in the DOM/input value after
    // the account has been created.
    if (passwordInput) passwordInput.value = '';
    refreshDashboard();
  } catch (err) {
    alert(err.message);
    if (passwordInput) passwordInput.value = '';
  }
}
