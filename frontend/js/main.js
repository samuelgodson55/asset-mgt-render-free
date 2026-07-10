// =============================================================================
// js/main.js
// -----------------------------------------------------------------------------
// The ONLY module loaded directly by every HTML page
// (`<script type="module" src="js/main.js"></script>`). Its job is purely
// wiring: it imports every other module and, on DOMContentLoaded, attaches
// real `addEventListener` calls for every interactive element on the page --
// there are NO `onclick="..."` / `onchange="..."` attributes left anywhere
// in the HTML or in any generated table-row markup.
//
// Static, one-off elements (the login form, the create-pool form, etc.) are
// wired individually by id, exactly like before. Buttons that appear in
// dynamically-rendered table rows (Properties Hub, Delete Profile, Process
// Return, pagination Prev/Next, etc.) can't be wired individually since they
// don't exist yet when this file runs and are re-created every time a table
// re-renders -- those use EVENT DELEGATION instead: a single listener on
// `document.body` reads the `data-action` (and any `data-*` args) off the
// element the user actually clicked/changed via `event.target.closest(...)`,
// then dispatches to the matching function in the CLICK_ACTIONS /
// CHANGE_ACTIONS registries below. Adding a new action anywhere in the app
// is then just: give the element `data-action="my-thing"` in its template
// string, and add `'my-thing': (el) => ...` to the relevant registry.
// =============================================================================

import { checkAccess, startIdleWatchdog, login, redirectByUserRole, logout, getSession } from './auth.js';
import { closeModal, switchTab, toggleRoute, toggleCapacityEdit, toggleNameEdit, changePage, setSearch, setPerPage, openRowDetailsFromElement } from './ui.js';
import { refreshDashboard, checkAlertsNow } from './dashboard.js';
import { dismissOverdueAlert } from './components/overdue.js';
import { dismissDueSoonAlert } from './components/due-soon.js';

import {
  openDispatchModal, submitDispatchForm, openPropsModal, recallException,
  saveCapacity, saveName, submitExceptionForm, submitCreatePoolForm, submitCsvImportForm,
  deleteAssetPool, setAssetsSearch, setAssetsPerPage, changeAssetsPage,
} from './components/assets.js';
import {
  deleteProfile, submitCreateUserForm, setUsersSearch, setUsersPerPage, changeUsersPage,
  openResetPasswordModal, submitResetPasswordForm,
  setDeletedUsersSearch, setDeletedUsersPerPage, changeDeletedUsersPage, restoreUser,
} from './components/users.js';
import { exportAuditLogs, changeAuditPage, setAuditPerPage } from './components/audit.js';
import { loadMyItems } from './components/myitems.js';
import {
  setOutsidersSearch, setOutsidersPerPage, changeOutsidersPage,
} from './components/outsiders.js';
import {
  openCustodyModal, processReturn, updateCustodySelection, toggleSelectAllCustody,
  processAllReturns, bulkProcessReturns,
} from './components/custody.js';
import { openProfileModal, submitChangePasswordForm, ROLE_LABELS } from './components/profile.js';
import { exportMyItems, exportCustodyItems, exportAllUsers, exportAllOutsiders } from './components/exports.js';
import { openExtensionRequestModal, submitExtensionRequestForm, decideExtensionRequest, openDirectExtendModal, submitDirectExtendForm, dismissExtensionRequestsAlert } from './components/extensions.js';

// -----------------------------------------------------------------------------
// DELEGATED "CLICK" ACTIONS
// -----------------------------------------------------------------------------
// Each handler receives the matched element (the one carrying data-action),
// so it can read whatever data-* attributes it needs off it.
//
// 'change-page' is shared by every table's Prev/Next buttons (they all
// carry the same data-action + a data-key telling us which table). Assets/
// Users/Outsiders/Audit now each do TRUE server-side pagination (their own
// `change*Page()` in their component file, mirroring components/audit.js),
// while My Items still pages a client-side cached array via ui.js's
// generic changePage() -- SERVER_PAGE_CHANGERS below is the lookup that
// routes to the right one for a given data-key.
const SERVER_PAGE_CHANGERS = {
  assets: changeAssetsPage,
  users: changeUsersPage,
  outsiders: changeOutsidersPage,
  deletedUsers: changeDeletedUsersPage,
};

const CLICK_ACTIONS = {
  'switch-tab': (el) => switchTab(el.dataset.tab),
  'close-modal': (el) => closeModal(el.dataset.modal),
  // Mobile-only "Details" button rendered by every table's component file
  // (see ui.js's rowDetailsTrigger()/openRowDetailsFromElement()) -- shows
  // whatever columns that table hides below the `sm` breakpoint.
  'open-row-details': (el) => openRowDetailsFromElement(el),
  'toggle-capacity-edit': () => toggleCapacityEdit(),
  'save-capacity': () => saveCapacity(),
  'toggle-name-edit': () => toggleNameEdit(),
  'save-name': () => saveName(),
  'open-dispatch': (el) => openDispatchModal(parseInt(el.dataset.assetId, 10), el.dataset.assetName, parseInt(el.dataset.available, 10)),
  'open-props': (el) => openPropsModal(parseInt(el.dataset.assetId, 10)),
  'recall-exception': (el) => recallException(parseInt(el.dataset.assetId, 10), parseInt(el.dataset.exceptionId, 10)),
  'open-custody': (el) => openCustodyModal(parseInt(el.dataset.entityId, 10), el.dataset.entityType),
  'process-return': (el) => processReturn(parseInt(el.dataset.checkoutId, 10)),
  'process-all-returns': () => processAllReturns(),
  'bulk-process-returns': () => bulkProcessReturns(),
  'delete-profile': (el) => deleteProfile(parseInt(el.dataset.userId, 10), el.dataset.userName),
  'reset-password': (el) => openResetPasswordModal(parseInt(el.dataset.userId, 10), el.dataset.userName),
  'restore-user': (el) => restoreUser(parseInt(el.dataset.userId, 10), el.dataset.userName),
  'delete-asset-pool': (el) => deleteAssetPool(parseInt(el.dataset.assetId, 10), el.dataset.assetName),
  'change-page': (el) => {
    const key = el.dataset.key;
    const delta = parseInt(el.dataset.delta, 10);
    const serverChanger = SERVER_PAGE_CHANGERS[key];
    if (serverChanger) serverChanger(delta);
    else changePage(key, delta);
  },
  'open-profile': () => openProfileModal(),
  'check-alerts': () => checkAlertsNow(),
  'dismiss-overdue-alert': () => dismissOverdueAlert(),
  'dismiss-due-soon-alert': () => dismissDueSoonAlert(),
  'dismiss-extension-requests-alert': () => dismissExtensionRequestsAlert(),
  'dismiss-all-clear-banner': () => {
    const banner = document.getElementById('alertsAllClearBanner');
    if (banner) banner.classList.add('hidden');
  },

  // Due-date extension requests -- see components/extensions.js.
  'open-extension-request': (el) => openExtensionRequestModal(parseInt(el.dataset.checkoutId, 10), el.dataset.assetName, el.dataset.dueDate),
  'approve-extension': (el) => decideExtensionRequest(parseInt(el.dataset.requestId, 10), true),
  'deny-extension': (el) => decideExtensionRequest(parseInt(el.dataset.requestId, 10), false),
  'open-direct-extend': (el) => openDirectExtendModal(parseInt(el.dataset.checkoutId, 10), el.dataset.assetName, el.dataset.dueDate),

  // Properties-assigned exports (CSV/PDF) -- see components/exports.js.
  'export-my-items': (el) => exportMyItems(el.dataset.format),
  'export-custody': (el) => exportCustodyItems(el.dataset.format),
  'export-all-users': (el) => exportAllUsers(el.dataset.format),
  'export-all-outsiders': (el) => exportAllOutsiders(el.dataset.format),

  // The audit ledger pages itself server-side (true limit/offset re-fetch
  // on every click) rather than through the shared client-side
  // tableState/changePage() machinery used by My Items -- see
  // components/audit.js's module docstring for why.
  'change-audit-page': (el) => changeAuditPage(parseInt(el.dataset.delta, 10)),
};

// -----------------------------------------------------------------------------
// DELEGATED "CHANGE" ACTIONS (checkboxes/selects rendered dynamically)
// -----------------------------------------------------------------------------
const CHANGE_ACTIONS = {
  'update-custody-selection': () => updateCustodySelection(),
  'toggle-select-all-custody': (el) => toggleSelectAllCustody(el),
  'toggle-route': () => toggleRoute(),
  'set-audit-perpage': (el) => setAuditPerPage(el.value),
};

function wireDelegatedEvents() {
  document.body.addEventListener('click', (event) => {
    const el = event.target.closest('[data-action]');
    if (!el) return;
    const action = CLICK_ACTIONS[el.dataset.action];
    if (action) action(el);
  });

  document.body.addEventListener('change', (event) => {
    const el = event.target.closest('[data-action]');
    if (!el) return;
    const action = CHANGE_ACTIONS[el.dataset.action];
    if (action) action(el);
  });
}

// -----------------------------------------------------------------------------
// SEARCH BOX / ROWS-PER-PAGE WIRING
// -----------------------------------------------------------------------------
// Connects any search <input> or "Rows per page" <select> present on the
// current page to the right setter. Assets/Users/Outsiders now do TRUE
// server-side search + pagination (their `set*Search()` is debounced and
// re-fetches from the API -- see components/assets.js, components/
// users.js, components/outsiders.js), while My Items still uses ui.js's
// generic client-side setSearch()/setPerPage() (it filters/paginates an
// already-downloaded array in memory, which is fine for one user's own,
// inherently small, custody list). Wrapped in `if (el)` checks throughout
// so this safely does nothing on pages that don't have a particular table
// (e.g. staff.html has no asset table, so `assetSearchInput` simply won't
// be found there).
function wireTableControls() {
  const serverDrivenControls = [
    { searchId: 'assetSearchInput', perPageId: 'assetPerPageSelect', setSearch: setAssetsSearch, setPerPage: setAssetsPerPage },
    { searchId: 'userSearchInput', perPageId: 'userPerPageSelect', setSearch: setUsersSearch, setPerPage: setUsersPerPage },
    { searchId: 'outsiderSearchInput', perPageId: 'outsiderPerPageSelect', setSearch: setOutsidersSearch, setPerPage: setOutsidersPerPage },
    { searchId: 'deletedUserSearchInput', perPageId: 'deletedUserPerPageSelect', setSearch: setDeletedUsersSearch, setPerPage: setDeletedUsersPerPage },
  ];
  serverDrivenControls.forEach(({ searchId, perPageId, setSearch: setServerSearch, setPerPage: setServerPerPage }) => {
    const searchInput = document.getElementById(searchId);
    if (searchInput) {
      searchInput.addEventListener('input', () => setServerSearch(searchInput.value));
    }
    const perPageSelect = document.getElementById(perPageId);
    if (perPageSelect) {
      perPageSelect.addEventListener('change', () => setServerPerPage(perPageSelect.value));
    }
  });

  // My Items: unchanged client-side path.
  const myItemsSearchInput = document.getElementById('myItemsSearchInput');
  if (myItemsSearchInput) {
    myItemsSearchInput.addEventListener('input', () => setSearch('myItems', myItemsSearchInput.value));
  }
  const myItemsPerPageSelect = document.getElementById('myItemsPerPageSelect');
  if (myItemsPerPageSelect) {
    myItemsPerPageSelect.addEventListener('change', () => setPerPage('myItems', myItemsPerPageSelect.value));
  }
}

// -----------------------------------------------------------------------------
// CSV IMPORT DRAG-AND-DROP
// -----------------------------------------------------------------------------
// BUG THIS FIXES: the drop zone in admin.html is just a styled <label> that
// wraps a hidden <input type="file"> -- that combination gives you the
// CLICK behavior for free (clicking anywhere on a <label> activates the
// <input> it's tied to, which is why "browse" already worked), but
// browsers do NOT wire up drag-and-drop for you just because a label LOOKS
// like a drop zone. Without explicit `dragover`/`drop` listeners, dropping
// a file onto that label does nothing at all (worse, the browser's default
// behavior is to navigate the whole tab to the dropped file).
//
// This function makes the drop zone actually work by:
//   1. Calling `event.preventDefault()` on `dragover` -- this is REQUIRED;
//      without it, the browser refuses to fire a `drop` event at all (its
//      default action for a dragover is "this element does not accept
//      drops").
//   2. Toggling a highlight style on `dragenter`/`dragleave` so the user
//      gets visual feedback while dragging a file over the zone.
//   3. On `drop`, preventing the browser's default "open this file in the
//      tab" behavior, then copying the dropped file(s) onto the actual
//      `<input type="file">` element via the same `DataTransfer` object
//      the input already understands -- `fileInput.files = event
//      .dataTransfer.files`. This is the standard, documented way to
//      programmatically set a file input's value (you can't just assign
//      a File object directly; the API insists on a FileList, and
//      `DataTransfer.files` already conveniently IS one).
//   4. Re-using the exact same `change` event dispatch the click/browse
//      path already relies on (see the listener registered on
//      `csvFileInput` right above), so both paths funnel through IDENTICAL
//      validation/upload code -- there's only one code path to keep
//      correct, not two.
function wireCsvDragAndDrop(fileInput, form) {
  const dropZone = document.getElementById('csvDropZone');
  if (!dropZone || !fileInput || !form) return; // Not on this page -- nothing to wire.

  const HIGHLIGHT_CLASSES = ['border-blue-500', 'bg-blue-500/10'];

  const highlight = () => dropZone.classList.add(...HIGHLIGHT_CLASSES);
  const unhighlight = () => dropZone.classList.remove(...HIGHLIGHT_CLASSES);

  // dragenter/dragover must BOTH preventDefault(), or the browser will
  // never consider this a valid drop target and the 'drop' event won't fire.
  dropZone.addEventListener('dragenter', (event) => {
    event.preventDefault();
    highlight();
  });
  dropZone.addEventListener('dragover', (event) => {
    event.preventDefault();
    highlight();
  });
  dropZone.addEventListener('dragleave', (event) => {
    event.preventDefault();
    // A single <label> can contain child elements (the icon/text above),
    // and the browser fires dragleave/dragenter as the pointer crosses
    // each child's boundary too -- only actually unhighlight once the
    // pointer has left the drop zone itself, not just moved between its
    // children.
    if (!dropZone.contains(event.relatedTarget)) unhighlight();
  });
  dropZone.addEventListener('drop', (event) => {
    event.preventDefault(); // Stop the browser from navigating to the dropped file.
    unhighlight();

    const droppedFiles = event.dataTransfer && event.dataTransfer.files;
    if (!droppedFiles || droppedFiles.length === 0) return;

    fileInput.files = droppedFiles;
    // The click/browse path fires this automatically when a user picks a
    // file via the OS dialog; a programmatic assignment to `.files` does
    // NOT fire it on its own, so dispatch it manually to trigger the same
    // "auto-submit on file selected" listener registered earlier.
    fileInput.dispatchEvent(new Event('change'));
  });
}

// -----------------------------------------------------------------------------
// PAGE BOOTSTRAP
// -----------------------------------------------------------------------------
document.addEventListener('DOMContentLoaded', () => {
  checkAccess();
  startIdleWatchdog();
  wireDelegatedEvents();

  // --- Login form (index.html) ---
  const loginForm = document.getElementById('login-form');
  if (loginForm) {
    loginForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      // Data Quality & Usability requirement #6: this single field now
      // accepts EITHER an email address OR a username -- see auth.js's
      // login() and the backend's schemas/auth.py -> LoginRequest.identifier.
      const identifier = document.getElementById('login-email').value;
      const password = document.getElementById('login-password').value;
      try {
        const data = await login(identifier, password);
        redirectByUserRole(data.role);
      } catch (error) {
        alert(error.message);
      }
    });
  }

  // --- Logout button (dashboards) ---
  const logoutBtn = document.getElementById('logout-btn');
  if (logoutBtn) {
    logoutBtn.addEventListener('click', (e) => { e.preventDefault(); logout(); });
  }

  // --- Dashboard forms (only present on admin.html / manager.html) ---
  const createPoolForm = document.getElementById('createPoolForm');
  if (createPoolForm) createPoolForm.addEventListener('submit', submitCreatePoolForm);

  const csvImportForm = document.getElementById('csvImportForm');
  if (csvImportForm) csvImportForm.addEventListener('submit', submitCsvImportForm);

  // The CSV file input auto-submits its form the moment a file is chosen,
  // rather than requiring a separate "Upload" click.
  const csvFileInput = document.getElementById('csvFileInput');
  if (csvFileInput && csvImportForm) {
    csvFileInput.addEventListener('change', () => csvImportForm.requestSubmit());
  }
  wireCsvDragAndDrop(csvFileInput, csvImportForm);

  const createUserForm = document.getElementById('createUserForm');
  if (createUserForm) createUserForm.addEventListener('submit', submitCreateUserForm);

  const changePasswordForm = document.getElementById('changePasswordForm');
  if (changePasswordForm) changePasswordForm.addEventListener('submit', submitChangePasswordForm);

  const resetPasswordForm = document.getElementById('resetPasswordForm');
  if (resetPasswordForm) resetPasswordForm.addEventListener('submit', submitResetPasswordForm);

  const dispatchForm = document.getElementById('dispatchForm');
  if (dispatchForm) dispatchForm.addEventListener('submit', submitDispatchForm);

  const exceptionForm = document.getElementById('exceptionForm');
  if (exceptionForm) exceptionForm.addEventListener('submit', submitExceptionForm);

  const extensionRequestForm = document.getElementById('extensionRequestForm');
  if (extensionRequestForm) extensionRequestForm.addEventListener('submit', submitExtensionRequestForm);

  const directExtendForm = document.getElementById('directExtendForm');
  if (directExtendForm) directExtendForm.addEventListener('submit', submitDirectExtendForm);

  const exportBtn = document.getElementById('exportAuditBtn');
  if (exportBtn) exportBtn.addEventListener('click', () => exportAuditLogs('csv'));

  const exportPdfBtn = document.getElementById('exportAuditPdfBtn');
  if (exportPdfBtn) exportPdfBtn.addEventListener('click', () => exportAuditLogs('pdf'));

  // --- Search boxes / rows-per-page selects on whichever tables exist ---
  wireTableControls();

  // --- Populate the navbar name/role + dashboard tables on any dashboard page ---
  const session = getSession();
  if (session) {
    const navName = document.getElementById('navUserName');
    if (navName) navName.textContent = session.name;
    const navInitials = document.getElementById('navUserInitials');
    if (navInitials) navInitials.textContent = session.name.split(' ').map(p => p[0]).join('').slice(0, 2).toUpperCase();

    // Account-type label under the name (e.g. "Super Admin", "Manager").
    // Set it immediately from the JWT's own `role` claim -- decoded
    // client-side already by getSession(), no extra request needed -- so
    // every dashboard shows the real, currently-logged-in account's type
    // instead of a hardcoded placeholder. On staff.html/customer.html,
    // loadMyItems() below then refines this further with the more specific
    // `department_role` once it comes back from the API (department_role
    // isn't embedded in the JWT itself -- see auth_service.py).
    const navRole = document.getElementById('myProfileRole');
    if (navRole) navRole.textContent = ROLE_LABELS[session.role] || session.role;

    if (document.getElementById('assetTableBody') || document.getElementById('userTableBody')) {
      refreshDashboard();
    }
    if (document.getElementById('myItemsTableBody')) {
      loadMyItems();
    }
  }
});
