// =============================================================================
// js/components/assets.js
// -----------------------------------------------------------------------------
// "Asset Inventory" table + the Dispatch (Issue/Checkout) drawer + the
// Properties Hub / Asset Pool Details modal (capacity edit, isolate/recall
// exceptions). Everything in this file is about AssetType pools.
// =============================================================================

import { apiRequest } from '../api.js';
import { escapeHtml, openModal, closeModal, toggleRoute, toggleCapacityEdit, toggleNameEdit, toggleDepartmentEdit, statusBadge, debounce, renderServerPaginationBar, rowDetailsTrigger } from '../ui.js';
import { refreshDashboard } from '../dashboard.js';

let currentDispatchAssetId = null; // remembers which asset the open dispatch drawer is for
let currentPropsAssetId = null;    // remembers which asset the open properties hub is for

// custody.js needs to know which asset's Properties Hub is open (if any) so
// that processReturn() can refresh it after a return -- exposed as a getter
// rather than a mutable export so ownership of the variable stays here.
export function getCurrentPropsAssetId() {
  return currentPropsAssetId;
}

// ---- Asset Inventory table ----
// TRUE server-side search + pagination (same pattern as components/
// audit.js's `auditState` -- see js/ui.js's module docstring on
// `tableState` for why this moved off the old client-side
// fetch-everything-once-then-filter-in-memory approach): every keystroke
// in the search box (debounced), page turn, or "rows per page" change
// re-fetches just that slice from `GET /assets?search=&limit=&offset=`
// instead of re-filtering an already-downloaded array.
const assetsState = { page: 1, perPage: 5, search: '', total: 0 };

export async function loadAssets() {
  const tbody = document.getElementById('assetTableBody');
  if (!tbody) return; // this page doesn't have an asset table
  try {
    const offset = (assetsState.page - 1) * assetsState.perPage;
    const params = new URLSearchParams({ limit: assetsState.perPage, offset });
    if (assetsState.search.trim()) params.set('search', assetsState.search.trim());
    const result = await apiRequest(`/assets?${params.toString()}`);
    assetsState.total = result.total;
    renderAssetsTable(result.items);
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="4" class="px-5 py-6 text-center text-rose-400">${escapeHtml(err.message)}</td></tr>`;
  }
}

function renderAssetsTable(items) {
  const tbody = document.getElementById('assetTableBody');
  if (!tbody) return;
  const isManagerView = document.body.dataset.view === 'manager';
  const isSuperAdminView = document.body.dataset.view === 'admin';

  document.querySelectorAll('.asset-pool-count').forEach(el => el.textContent = assetsState.total);

  tbody.innerHTML = items.map(a => {
    // Built once, reused in two places: inline at the end of the row for
    // desktop (`sm:table-cell`, unchanged from before) AND as a full-width
    // "Actions" block inside the mobile row-details popup (see below) --
    // that column is now hidden on phones instead of overflowing off the
    // right edge of the screen, so these same buttons have to be reachable
    // some other way once a row is tapped.
    const actionButtons = `
      ${isManagerView
        ? `<button data-action="open-props" data-asset-id="${a.id}" class="rounded-md px-2.5 py-1.5 text-[12px] font-medium text-slate-400 underline-offset-2 transition hover:text-blue-400 hover:underline">View Pool details</button>`
        : `<button data-action="open-props" data-asset-id="${a.id}" class="rounded-md border border-border px-2.5 py-1.5 text-[12px] font-medium text-slate-300 transition hover:border-slate-500 hover:text-white">Properties Hub</button>`}
      <button data-action="open-dispatch" data-asset-id="${a.id}" data-asset-name="${escapeHtml(a.name)}" data-available="${a.available_quantity}" class="rounded-md bg-blue-600/90 px-2.5 py-1.5 text-[12px] font-medium text-white transition hover:bg-blue-500">Issue / Dispatch</button>
      ${isSuperAdminView
        ? `<button data-action="delete-asset-pool" data-asset-id="${a.id}" data-asset-name="${escapeHtml(a.name)}" class="rounded-md border border-rose-500/30 px-2.5 py-1.5 text-[12px] font-medium text-rose-400 transition hover:border-rose-500 hover:bg-rose-500/10">Delete</button>`
        : ''}`;

    // The ENTIRE row is tappable on mobile (rowDetailsTrigger() attaches
    // `data-action="open-row-details"` straight to the <tr>) instead of a
    // separate small "Details" button eating space next to the name --
    // tapping anywhere on a row's name/status cells pops the shared
    // details modal with the Available/Total count plus these same action
    // buttons. On desktop, every column still renders inline exactly as
    // before, so real buttons under the pointer are always what actually
    // gets clicked (closest('[data-action]') in main.js's delegated
    // handler resolves to the innermost match), and the row-level handler
    // is simply never reached there.
    return `
    <tr ${rowDetailsTrigger(escapeHtml(a.name), [
      ['Available / Total', `${a.available_quantity} / ${a.total_quantity} units`],
      ...(a.department ? [['Department', escapeHtml(a.department)]] : []),
      ['', `<div class="flex flex-wrap gap-2">${actionButtons}</div>`],
    ])} class="cursor-pointer transition hover:bg-card2/40 active:bg-card2/60 sm:cursor-default">
    <td class="px-5 py-3.5">
        <div class="flex items-center gap-2">
          <div>
            <p class="font-medium text-slate-100">${escapeHtml(a.name)}</p>
            <p class="tag-mono text-[11px] text-slate-500">POOL-${a.id}${a.department ? ` · ${escapeHtml(a.department)}` : ''}</p>
          </div>
          <!-- Mobile-only affordance showing the row itself is tappable
               (replaces the old separate "Details" button). -->
          <svg class="ml-auto h-4 w-4 shrink-0 text-slate-600 sm:hidden" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 18l6-6-6-6"/></svg>
        </div>
      </td>
      <td class="hidden px-5 py-3.5 tag-mono text-slate-300 sm:table-cell">${a.available_quantity} / ${a.total_quantity} units</td>
      <td class="px-5 py-3.5">${statusBadge(a.available_quantity)}</td>
      <td class="hidden px-5 py-3.5 sm:table-cell">
        <div class="flex flex-wrap justify-end gap-2">${actionButtons}</div>
      </td>
    </tr>`;
  }).join('') || `<tr><td colspan="4" class="px-5 py-6 text-center text-slate-500">No asset pools found.</td></tr>`;

  renderServerPaginationBar('assets', assetsState);
}

// Called from the search box's 'input' listener (main.js), debounced.
export const setAssetsSearch = debounce((value) => {
  assetsState.search = value;
  assetsState.page = 1; // always jump back to page 1 on a new search
  loadAssets();
});

// Called from the "Rows per page" <select>'s 'change' listener (main.js).
export function setAssetsPerPage(value) {
  assetsState.perPage = parseInt(value, 10) || 5;
  assetsState.page = 1;
  loadAssets();
}

// Called by main.js's delegated click handler when Prev/Next is clicked.
export function changeAssetsPage(delta) {
  const nextPage = assetsState.page + delta;
  if (nextPage < 1) return;
  assetsState.page = nextPage;
  loadAssets();
}

// ---- Delete Asset Pool (Super Admin only) ----
// Backend endpoint (DELETE /assets/{id}, see backend/api/assets.py ->
// services/asset_service.py's delete_asset_type) already existed and was
// already enforced as Super Admin-only + soft-delete + "no outstanding
// checkouts or isolated units" -- there was just no button in the UI to
// reach it. This wires the Inventory table's row-level "Delete" action
// (added above in renderAssetsTable) to it, mirroring
// components/users.js's deleteProfile().
export async function deleteAssetPool(assetId, assetName) {
  if (!confirm(`Delete asset pool "${assetName}"? This cannot be undone, and only pools with no outstanding checkouts or isolated units can be deleted.`)) return;
  try {
    const result = await apiRequest(`/assets/${assetId}`, { method: 'DELETE' });
    alert(result.message);
    // If the Properties Hub for this exact pool happens to be open, close
    // it -- it would otherwise be showing details for a pool that no
    // longer exists in active inventory.
    if (currentPropsAssetId === assetId) closeModal('propsModal');
    refreshDashboard();
  } catch (err) {
    alert(err.message);
  }
}

// ---- Dispatch (Issue / Checkout) drawer ----

// Operations & Observability requirement #4: client-side min/max date
// validation. Keeps this in one place so it can't drift out of sync with
// the backend's matching check in schemas/assets.py's
// AdvancedCheckoutRequest._validate_due_date (which enforces the SAME
// rule server-side -- this client-side version is purely a UX nicety that
// stops the browser's own date picker from ever offering an invalid date
// in the first place; it is NOT the actual security boundary).
const MAX_DUE_DATE_YEARS_AHEAD = 5;

function todayAsInputValue() {
  // <input type="date"> wants "YYYY-MM-DD" in the LOCAL timezone -- using
  // toISOString() directly here would be wrong for anyone west of UTC
  // (it could show "yesterday" as the earliest selectable date right
  // after midnight local time), so we build the string from local
  // getFullYear/getMonth/getDate instead.
  const d = new Date();
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  return `${yyyy}-${mm}-${dd}`;
}

function applyDueDateBounds(inputEl) {
  if (!inputEl) return;
  const today = new Date();
  const min = todayAsInputValue();
  const maxDate = new Date(today.getFullYear() + MAX_DUE_DATE_YEARS_AHEAD, today.getMonth(), today.getDate());
  const max = `${maxDate.getFullYear()}-${String(maxDate.getMonth() + 1).padStart(2, '0')}-${String(maxDate.getDate()).padStart(2, '0')}`;
  inputEl.min = min;
  inputEl.max = max;
}

export function openDispatchModal(assetId, assetName, available) {
  currentDispatchAssetId = assetId;
  document.getElementById('dispatchAssetName').textContent = `${assetName} · ${available} available`;
  document.getElementById('dispatchForm').reset();
  // Re-applied every time the drawer opens (rather than once at page load)
  // so "today" is always correct even if the dashboard has been left open
  // across midnight.
  applyDueDateBounds(document.getElementById('dispatchDueDate'));
  toggleRoute();
  openModal('dispatchModal');
}

export async function submitDispatchForm(event) {
  event.preventDefault();
  if (!currentDispatchAssetId) return;

  const routeVal = document.getElementById('routeSelect').value;
  const quantity = parseInt(document.getElementById('dispatchQuantity').value, 10) || 1;
  const dueDate = document.getElementById('dispatchDueDate').value || null;

  let payload = { quantity, due_date: dueDate };

  // NOTE: this dispatch form is shared identically by admin.html AND
  // manager.html -- Managers get all three "Assign To" routes (Staff,
  // Linked Customer Account, Ad-Hoc Individual) exactly like a Super
  // Admin, enforced by the backend's `require_privileged_role` on
  // POST /assets/{id}/checkout_advanced (see backend/api/assets.py).
  if (routeVal === 'staff') {
    payload.assignee_type = 'user';
    payload.user_id = parseInt(document.getElementById('staffSelect').value, 10);
  } else if (routeVal === 'customer') {
    // A "Linked Customer Account" is a real, login-capable role="customer"
    // User row (populated into #customerSelect by components/users.js's
    // loadUsers()) -- NOT a models.Outsider record. This mirrors the
    // 'staff' branch above exactly (same assignee_type='user' + user_id
    // shape), just sourced from a dropdown narrowed to customer accounts
    // instead of the full roster.
    payload.assignee_type = 'user';
    payload.user_id = parseInt(document.getElementById('customerSelect').value, 10);
    if (!payload.user_id) {
      alert('No linked customer accounts are on file yet. Create one from the User Directory first.');
      return;
    }
  } else {
    payload.assignee_type = 'outsider';
    payload.outsider_name = document.getElementById('adhocName').value;
    payload.outsider_company = document.getElementById('adhocCompany').value;
    payload.outsider_contact = document.getElementById('adhocContact').value;
  }

  try {
    const result = await apiRequest(`/assets/${currentDispatchAssetId}/checkout_advanced`, {
      method: 'POST', body: JSON.stringify(payload),
    });
    alert(result.message);
    closeModal('dispatchModal');
    refreshDashboard();
  } catch (err) {
    alert(err.message);
  }
}

// ---- Properties Hub / Asset Pool Details modal ----
export async function openPropsModal(assetId) {
  currentPropsAssetId = assetId;
  try {
    const details = await apiRequest(`/assets/${assetId}/details`);
    document.getElementById('propsAssetName').textContent = details.name;
    document.getElementById('propsTotal').textContent = details.total_quantity;
    document.getElementById('propsAvailable').textContent = details.available_quantity;

    // Originating department (optional -- see models.py's AssetType
    // docstring). manager.html's read-only Properties Hub hides this row
    // entirely when no department is set on this pool, same "hide the row
    // rather than show a blank/em-dash" pattern used by
    // profileDepartmentRow (see components/profile.js).
    const deptRow = document.getElementById('propsDepartmentRow');
    if (deptRow) {
      if (details.department) {
        document.getElementById('propsDepartment').textContent = details.department;
        deptRow.classList.remove('hidden');
      } else {
        deptRow.classList.add('hidden');
      }
    }

    // admin.html's editable version of the same field -- only present
    // there (deptInput doesn't exist on manager.html). Unlike the
    // read-only row above, this ALWAYS shows (so a pool with no
    // department yet has somewhere to add one), falling back to a plain
    // "No department set" placeholder instead of hiding.
    const deptInput = document.getElementById('deptInput');
    if (deptInput) {
      document.getElementById('propsDepartment').textContent = details.department || 'No department set';
      deptInput.value = details.department || '';
    }
    // Reset back to display mode (not mid-edit) every time the modal is
    // (re)opened, same reasoning as nameDisplay/nameEdit below.
    const deptDisplay = document.getElementById('deptDisplay');
    const deptEdit = document.getElementById('deptEdit');
    if (deptDisplay && deptEdit) {
      deptDisplay.classList.remove('hidden');
      deptEdit.classList.add('hidden');
      deptEdit.classList.remove('flex');
    }
    // Outbound + Isolated now come straight from the backend's derived
    // Available = Total - Outbound - Isolated calculation (requirement #3)
    // instead of being re-derived here from possibly-stale numbers.
    document.getElementById('propsOutbound').textContent = details.outbound_quantity;
    document.getElementById('propsRepair').textContent = details.isolated_quantity;

    // Only present on admin.html (Super Admin's Properties Hub) -- the
    // manager.html read-only version of this modal has no delete button.
    const deleteBtn = document.getElementById('propsDeleteBtn');
    if (deleteBtn) {
      deleteBtn.dataset.assetId = assetId;
      deleteBtn.dataset.assetName = details.name;
    }

    const capacityInput = document.getElementById('capacityInput');
    if (capacityInput) capacityInput.value = details.total_quantity;

    // Rename field -- only present on admin.html (Super Admin's Properties
    // Hub), same as capacityInput above. Reset back to display mode (not
    // mid-edit) every time the modal is (re)opened, so a stale edit state
    // from a previous visit never lingers.
    const nameInput = document.getElementById('nameInput');
    if (nameInput) nameInput.value = details.name;
    const nameDisplay = document.getElementById('nameDisplay');
    const nameEdit = document.getElementById('nameEdit');
    if (nameDisplay && nameEdit) {
      nameDisplay.classList.remove('hidden');
      nameEdit.classList.add('hidden');
      nameEdit.classList.remove('flex');
    }

    const list = document.getElementById('propsDeploymentList');
    list.innerHTML = details.active_assignments.map(a => `
    <div class="flex items-center justify-between rounded-lg border border-border bg-card2/50 px-3 py-2.5">
      <div>
        <p class="text-[13px] font-medium text-slate-200">${escapeHtml(a.assignee_name)} <span class="text-slate-500">(${escapeHtml(a.assignee_type)})</span></p>
        <p class="text-[11px] text-slate-500">Outstanding ${a.outstanding} / ${a.quantity} · due ${escapeHtml(a.due_date)}</p>
      </div>
      <div class="flex items-center gap-2">
        <input type="number" min="1" max="${a.outstanding}" value="${a.outstanding}" id="returnQty-${a.checkout_id}"
          class="w-16 rounded-md border border-border bg-card2 px-2 py-1.5 text-[12px] text-slate-200 outline-none focus:border-emerald-500/60" />
        <button data-action="process-return" data-checkout-id="${a.checkout_id}" class="rounded-md bg-emerald-600/90 px-2.5 py-1.5 text-[11px] font-semibold text-white transition hover:bg-emerald-500">Process Return</button>
      </div>
    </div>`).join('') || `<p class="text-[12px] text-slate-500">No active deployments for this pool.</p>`;

    // Isolated units (Under Repair / Stolen / Missing) with a Recall
    // action -- requirement #3's "Recall and Update" workflow.
    const isolatedItems = [...details.under_repair_items, ...details.stolen_items];
    const isolatedList = document.getElementById('propsIsolatedList');
    if (isolatedList) {
      isolatedList.innerHTML = isolatedItems.map(item => `
    <div class="flex items-center justify-between rounded-lg border border-amber-500/20 bg-amber-500/5 px-3 py-2.5">
      <div>
        <p class="tag-mono text-[12px] font-medium text-slate-200">${escapeHtml(item.serial)}</p>
        <p class="text-[11px] text-slate-500">${escapeHtml(item.notes || 'No notes on file')}</p>
      </div>
      <button data-action="recall-exception" data-asset-id="${assetId}" data-exception-id="${item.exception_id}" class="rounded-md border border-emerald-500/40 px-2.5 py-1.5 text-[11px] font-semibold text-emerald-400 transition hover:bg-emerald-500/10">Recall to Service</button>
    </div>`).join('') || `<p class="text-[12px] text-slate-500">No isolated units for this pool.</p>`;
    }

    openModal('propsModal');
  } catch (err) {
    alert(err.message);
  }
}

// ---- Recall an isolated unit (Under Repair / Stolen) back into service ----
export async function recallException(assetId, exceptionId) {
  try {
    const result = await apiRequest(`/assets/${assetId}/exception/${exceptionId}/recall`, { method: 'POST' });
    alert(result.message);
    openPropsModal(assetId);
    refreshDashboard();
  } catch (err) {
    alert(err.message);
  }
}

export async function saveCapacity() {
  const newTotal = parseInt(document.getElementById('capacityInput').value, 10);
  try {
    await apiRequest(`/assets/${currentPropsAssetId}/quantity`, {
      method: 'PUT', body: JSON.stringify({ new_total: newTotal }),
    });
    toggleCapacityEdit();
    openPropsModal(currentPropsAssetId);
    refreshDashboard();
  } catch (err) {
    alert(err.message);
  }
}

export async function saveName() {
  const newName = document.getElementById('nameInput').value.trim();
  if (!newName) {
    alert('Asset name cannot be empty.');
    return;
  }
  try {
    await apiRequest(`/assets/${currentPropsAssetId}/name`, {
      method: 'PUT', body: JSON.stringify({ name: newName }),
    });
    toggleNameEdit();
    openPropsModal(currentPropsAssetId);
    refreshDashboard();
  } catch (err) {
    alert(err.message);
  }
}

// Unlike saveName() above, an empty department input is a valid, deliberate
// "clear it back to unset" action rather than an error -- so this sends
// `null` instead of blocking the submit, letting an admin remove a
// department just as easily as adding one.
export async function saveDepartment() {
  const newDepartment = document.getElementById('deptInput').value.trim();
  try {
    await apiRequest(`/assets/${currentPropsAssetId}/department`, {
      method: 'PUT', body: JSON.stringify({ department: newDepartment || null }),
    });
    toggleDepartmentEdit();
    openPropsModal(currentPropsAssetId);
    refreshDashboard();
  } catch (err) {
    alert(err.message);
  }
}

export async function submitExceptionForm(event) {
  event.preventDefault();
  const payload = {
    serial_number: document.getElementById('exceptionSerial').value,
    status_label: document.getElementById('exceptionStatus').value,
    notes: document.getElementById('exceptionNotes').value,
  };
  try {
    await apiRequest(`/assets/${currentPropsAssetId}/exception`, { method: 'POST', body: JSON.stringify(payload) });
    document.getElementById('exceptionForm').reset();
    openPropsModal(currentPropsAssetId);
    refreshDashboard();
  } catch (err) {
    alert(err.message);
  }
}

// ---- Register New Inventory Pool (Super Admin only) ----
export async function submitCreatePoolForm(event) {
  event.preventDefault();
  const departmentInput = document.getElementById('newPoolDepartment');
  const payload = {
    name: document.getElementById('newPoolName').value,
    total_quantity: parseInt(document.getElementById('newPoolQty').value, 10),
    // Optional -- which internal department this pool's equipment
    // originates from. Left out entirely (null) rather than sent as an
    // empty string when the field is blank -- see schemas/assets.py's
    // AssetTypeCreate.department for the matching server-side
    // normalization.
    department: departmentInput && departmentInput.value.trim() ? departmentInput.value.trim() : null,
  };
  try {
    await apiRequest('/assets', { method: 'POST', body: JSON.stringify(payload) });
    document.getElementById('createPoolForm').reset();
    refreshDashboard();
  } catch (err) {
    alert(err.message);
  }
}

// ---- Asset Inventory Export (CSV / PDF, by department) ----
// Opens a small modal offering "Download All" plus one option per distinct
// department currently on file (see GET /assets/departments ->
// services/asset_service.py's list_asset_departments()), then hands the
// selected scope off to components/exports.js's exportAssetsInventory()
// to do the actual authenticated file download. Unlike the
// properties-assigned exports elsewhere in the app (which export WHO
// currently holds WHAT), this exports the Asset Inventory table itself --
// one row per pool.
export async function openAssetExportModal() {
  const select = document.getElementById('assetExportDepartment');
  if (!select) return;
  select.innerHTML = '<option value="all">Download All</option>';
  try {
    const result = await apiRequest('/assets/departments');
    (result.departments || []).forEach((dept) => {
      const opt = document.createElement('option');
      opt.value = dept;
      opt.textContent = dept;
      select.appendChild(opt);
    });
  } catch (err) {
    // Non-fatal -- "Download All" still works even if the department list
    // fails to load for some reason.
  }
  openModal('assetExportModal');
}

export async function submitCsvImportForm(event) {
  event.preventDefault();
  const fileInput = document.getElementById('csvFileInput');
  if (!fileInput.files.length) return;
  const formData = new FormData();
  formData.append('file', fileInput.files[0]);
  try {
    const result = await apiRequest('/assets/import', { method: 'POST', body: formData });

    // Data Quality & Usability requirement #5: the backend no longer
    // silently skips bad rows -- it returns a full diagnostic report
    // (`result.errors`) of exactly which rows were rejected and why. We
    // surface that here instead of only showing the success message, so a
    // Super Admin can immediately go fix the specific rows rather than
    // wondering why the imported count came in lower than the file's row
    // count.
    if (result.errors && result.errors.length) {
      const preview = result.errors
        .slice(0, 10)
        .map(e => `Row ${e.row}${e.name ? ` (${e.name})` : ''}: ${e.reason}`)
        .join('\n');
      const more = result.errors.length > 10
        ? `\n…and ${result.errors.length - 10} more rejected row(s).`
        : '';
      alert(`${result.message}\n\nRejected rows:\n${preview}${more}`);
    } else {
      alert(result.message);
    }
    fileInput.value = '';
    refreshDashboard();
  } catch (err) {
    alert(err.message);
  }
}
