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
import { closeModal, switchTab, toggleRoute, toggleCapacityEdit, toggleNameEdit, toggleCategoryEdit, togglePriceEdit, changePage, setSearch, setPerPage, openRowDetailsFromElement, initSwipeNav, initModalBackdropDismiss, switchDashboardTab, initDashSwipeNav } from './ui.js';
import { toggleTheme, initThemeToggle } from './theme.js';
import { refreshDashboard } from './dashboard.js';
import { initNotificationBell, toggleNotificationDropdown, closeNotificationDropdown, refreshNotifications } from './components/notifications.js';

import {
  openDispatchModal, submitDispatchForm, openPropsModal, recallException,
  saveCapacity, saveName, saveCategory, savePrice, submitExceptionForm, submitCreatePoolForm, submitCsvImportForm,
  deleteAssetPool, setAssetsSearch, setAssetsPerPage, changeAssetsPage, openAssetExportModal,
  downloadCsvImportTemplate,
} from './components/assets.js';
import {
  deleteProfile, submitCreateUserForm, setUsersSearch, setUsersPerPage, changeUsersPage,
  openResetPasswordModal, submitResetPasswordForm,
  setDeletedUsersSearch, setDeletedUsersPerPage, changeDeletedUsersPage, restoreUser,
  openEditUserModal, submitEditUserForm,
} from './components/users.js';
import { exportAuditLogs, changeAuditPage, setAuditPerPage } from './components/audit.js';
import { loadMyItems } from './components/myitems.js';
import {
  setOutsidersSearch, setOutsidersPerPage, changeOutsidersPage,
  openEditOutsiderModal, submitEditOutsiderForm,
} from './components/outsiders.js';
import {
  openCustodyModal, processReturn, updateCustodySelection, toggleSelectAllCustody,
  processAllReturns, bulkProcessReturns, openBulkExtendModal, submitBulkExtendForm,
} from './components/custody.js';
import { openProfileModal, submitChangePasswordForm, ROLE_LABELS } from './components/profile.js';
import { exportMyItems, exportCustodyItems, exportAllUsers, exportAllOutsiders, exportAssetsInventory, exportQuotation, exportQuoteDetail, exportMyQuoteDetail } from './components/exports.js';
import {
  initQuotationPage, addAssetToOrder, updateOrderItemQuantity, removeOrderItem,
  loadVatSetting, submitVatSettingsForm, loadPublicConfig, submitMyQuotation,
  initQuotesTab, setQuotesSearch, setQuotesPerPage, changeQuotesPage, openQuoteDetail,
  updateAdminQuoteItemQuantity, removeAdminQuoteItem, saveQuoteNotes, saveQuoteDiscount, addQuoteDetailItem,
  addQuoteOutsourcedItem, removeQuoteOutsourcedItem,
  searchAssignUsers, assignQuoteToUser, unassignQuote,
  switchQuotationTab, searchQuoteDetailAssets, selectQuoteDetailAsset, clearQuoteDetailAsset,
  openCreateQuoteModal, toggleQuoteRoute, submitCreateQuote,
  approveQuote, getCurrentQuoteId,
  openFulfillmentDrawer, updateFulfillmentSelection, toggleSelectAllFulfillment, processFulfillmentSelected,
  addShortfallAllocationRow, removeShortfallAllocationRow,
  openMyQuoteDetail, updateMyQuoteItemQuantity, removeMyQuoteItem,
  addMyQuoteDetailItem, searchMyQuoteDetailAssets, selectMyQuoteDetailAsset, clearMyQuoteDetailAsset,
  toggleQuoteAssignAdhocForm, submitQuoteAssignAdhoc,
} from './components/quotation.js';
import {
  refreshBackupsPanel,
  createBackupNow,
  downloadBackup,
  deleteBackup,
  openRestoreLocalModal,
  openRestoreUploadModal,
  confirmRestore,
} from './components/backups.js';
import { openExtensionRequestModal, submitExtensionRequestForm, decideExtensionRequest, openDirectExtendModal, submitDirectExtendForm, dismissMyExtensionDecisionsAlert, submitDenyReasonForm } from './components/extensions.js';

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
  quotes: changeQuotesPage,
};

const CLICK_ACTIONS = {
  'switch-tab': (el) => switchTab(el.dataset.tab),
  'switch-dash-tab': (el) => switchDashboardTab(el.dataset.tab),
  'close-modal': (el) => closeModal(el.dataset.modal),
  'toggle-theme': () => toggleTheme(),
  // Mobile-only "Details" button rendered by every table's component file
  // (see ui.js's rowDetailsTrigger()/openRowDetailsFromElement()) -- shows
  // whatever columns that table hides below the `sm` breakpoint.
  'open-row-details': (el) => openRowDetailsFromElement(el),
  'toggle-capacity-edit': () => toggleCapacityEdit(),
  'save-capacity': () => saveCapacity(),
  'toggle-name-edit': () => toggleNameEdit(),
  'save-name': () => saveName(),
  'toggle-category-edit': () => toggleCategoryEdit(),
  'save-category': () => saveCategory(),
  'toggle-price-edit': () => togglePriceEdit(),
  'save-price': () => savePrice(),
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
  'edit-user': (el) => openEditUserModal(parseInt(el.dataset.userId, 10)),
  'edit-outsider': (el) => openEditOutsiderModal(parseInt(el.dataset.outsiderId, 10)),
  'delete-asset-pool': (el) => deleteAssetPool(parseInt(el.dataset.assetId, 10), el.dataset.assetName),
  'open-asset-export': () => openAssetExportModal(),
  'download-csv-template': () => downloadCsvImportTemplate(),
  'change-page': (el) => {
    const key = el.dataset.key;
    const delta = parseInt(el.dataset.delta, 10);
    const serverChanger = SERVER_PAGE_CHANGERS[key];
    if (serverChanger) serverChanger(delta);
    else changePage(key, delta);
  },
  'open-profile': () => openProfileModal(),
  'toggle-notifications': () => toggleNotificationDropdown(),
  'close-notifications': () => closeNotificationDropdown(),
  'dismiss-my-extension-decisions-alert': () => dismissMyExtensionDecisionsAlert(),

  // Due-date extension requests -- see components/extensions.js.
  'open-extension-request': (el) => openExtensionRequestModal(parseInt(el.dataset.checkoutId, 10), el.dataset.assetName, el.dataset.dueDate),
  'approve-extension': (el) => decideExtensionRequest(parseInt(el.dataset.requestId, 10), true),
  'deny-extension': (el) => decideExtensionRequest(parseInt(el.dataset.requestId, 10), false),
  'open-direct-extend': (el) => openDirectExtendModal(parseInt(el.dataset.checkoutId, 10), el.dataset.assetName, el.dataset.dueDate),
  'open-bulk-extend': () => openBulkExtendModal(),

  // Properties-assigned exports (CSV/PDF) -- see components/exports.js.
  // `el` (the clicked button) is passed through as the second argument so
  // each export function can disable/relabel it to "Exporting..." for the
  // duration of the download, same progress feedback as the backup button.
  'export-my-items': (el) => exportMyItems(el.dataset.format, el),
  'export-custody': (el) => exportCustodyItems(el.dataset.format, el),
  'export-all-users': (el) => exportAllUsers(el.dataset.format, el),
  'export-all-outsiders': (el) => exportAllOutsiders(el.dataset.format, el),
  'export-assets-inventory': (el) => exportAssetsInventory(el.dataset.format, el),

  // Self-service Equipment Quotation -- see components/quotation.js.
  // `data-context` distinguishes the desktop table's inputs from the
  // mobile card layout's ("-m" suffixed) copies of the same asset row, so
  // Add always reads the quantity/date inputs actually visible to the
  // person who clicked it.
  'add-to-order': (el) => addAssetToOrder(parseInt(el.dataset.assetId, 10), el.dataset.context || ''),
  'remove-order-item': (el) => removeOrderItem(parseInt(el.dataset.itemId, 10)),
  'export-quotation': (el) => exportQuotation(el),
  'submit-quotation': (el) => submitMyQuotation(el),

  // Admin/Manager "Quotes" tab -- see components/quotation.js.
  'open-quote-detail': (el) => openQuoteDetail(parseInt(el.dataset.quoteId, 10)),
  'remove-admin-quote-item': (el) => removeAdminQuoteItem(parseInt(el.dataset.itemId, 10)),
  'remove-quote-outsourced-item': (el) => removeQuoteOutsourcedItem(parseInt(el.dataset.outsourcedItemId, 10)),
  'save-quote-notes': () => saveQuoteNotes(),
  'save-quote-discount': () => saveQuoteDiscount(),
  'add-quote-detail-item': () => addQuoteDetailItem(),
  'add-quote-outsourced-item': () => addQuoteOutsourcedItem(),
  'assign-quote-to-user': (el) => assignQuoteToUser(parseInt(el.dataset.userId, 10)),
  'unassign-quote': () => unassignQuote(),
  'toggle-quote-assign-adhoc': () => toggleQuoteAssignAdhocForm(),
  'submit-quote-assign-adhoc': () => submitQuoteAssignAdhoc(),
  'export-quote-detail': (el) => exportQuoteDetail(el),
  'export-my-quote-detail': (el) => exportMyQuoteDetail(el),
  'select-quote-detail-asset': (el) => selectQuoteDetailAsset(parseInt(el.dataset.assetId, 10), el.dataset.assetName),
  'clear-quote-detail-asset': () => clearQuoteDetailAsset(),
  'open-create-quote-modal': () => openCreateQuoteModal(),
  'submit-create-quote': (el) => submitCreateQuote(el),

  // Quote-to-Checkout workflow: approve (master queue row + Quote Detail
  // modal's own button) and the Fulfillment Drawer's bulk physical
  // checkout -- see components/quotation.js.
  'approve-quote': (el) => approveQuote(parseInt(el.dataset.quoteId, 10)),
  'approve-quote-detail': () => approveQuote(getCurrentQuoteId()),
  'open-fulfillment-drawer': () => openFulfillmentDrawer(),
  'process-fulfillment-selected': (el) => processFulfillmentSelected(el),
  'add-shortfall-row': (el) => addShortfallAllocationRow(el),
  'remove-shortfall-row': (el) => removeShortfallAllocationRow(el),

  // Self-service "My Order" / "My Quotes" tabs -- see components/quotation.js.
  'switch-quotation-tab': (el) => switchQuotationTab(el.dataset.tab),
  'open-my-quote-detail': (el) => openMyQuoteDetail(parseInt(el.dataset.quotationId, 10)),
  'remove-my-quote-item': (el) => removeMyQuoteItem(parseInt(el.dataset.itemId, 10)),
  'add-my-quote-detail-item': () => addMyQuoteDetailItem(),
  'select-my-quote-detail-asset': (el) => selectMyQuoteDetailAsset(parseInt(el.dataset.assetId, 10), el.dataset.assetName),
  'clear-my-quote-detail-asset': () => clearMyQuoteDetailAsset(),

  // The audit ledger pages itself server-side (true limit/offset re-fetch
  // on every click) rather than through the shared client-side
  // tableState/changePage() machinery used by My Items -- see
  // components/audit.js's module docstring for why.
  'change-audit-page': (el) => changeAuditPage(parseInt(el.dataset.delta, 10)),

  // System Backups panel -- see components/backups.js.
  'refresh-backups': () => refreshBackupsPanel(),
  'create-backup-now': (el) => createBackupNow(el),
  'download-backup': (el) => downloadBackup(el),
  'delete-backup': (el) => deleteBackup(el),
  'restore-local-backup': (el) => openRestoreLocalModal(el),
  'restore-upload-backup': () => openRestoreUploadModal(),
  'confirm-restore-backup': () => confirmRestore(),
};

// -----------------------------------------------------------------------------
// DELEGATED "CHANGE" ACTIONS (checkboxes/selects rendered dynamically)
// -----------------------------------------------------------------------------
const CHANGE_ACTIONS = {
  'update-custody-selection': () => updateCustodySelection(),
  'toggle-select-all-custody': (el) => toggleSelectAllCustody(el),
  'toggle-route': () => toggleRoute(),
  'toggle-quote-route': () => toggleQuoteRoute(),
  'set-audit-perpage': (el) => setAuditPerPage(el.value),
  'update-order-qty': (el) => updateOrderItemQuantity(parseInt(el.dataset.itemId, 10), el.value),
  'update-my-quote-item-qty': (el) => updateMyQuoteItemQuantity(parseInt(el.dataset.itemId, 10), el.value),
  'update-admin-quote-qty': (el) => updateAdminQuoteItemQuantity(parseInt(el.dataset.itemId, 10), el.value),
  'toggle-fulfillment-selection': () => updateFulfillmentSelection(),
  'toggle-select-all-fulfillment': (el) => toggleSelectAllFulfillment(el),
};

function wireDelegatedEvents() {
  document.body.addEventListener('click', (event) => {
    const el = event.target.closest('[data-action]');
    if (!el) return;
    const action = CLICK_ACTIONS[el.dataset.action];
    if (!action) return;

    // Action buttons embedded in the mobile "row details" popup's Actions
    // block (see ui.js's openRowDetailsFromElement()) each open ANOTHER
    // modal on top of it (Dispatch drawer, Properties Hub, Reset Password,
    // etc.) -- close the details popup first so the two never stack.
    if (el.dataset.action !== 'open-row-details' && el.closest('#rowDetailsBody')) {
      closeModal('rowDetailsModal');
    }
    action(el);
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
    { searchId: 'quotesSearchInput', perPageId: 'quotesPerPageSelect', setSearch: setQuotesSearch, setPerPage: setQuotesPerPage },
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

  // My Items + Quotation Catalog: client-side path (small, bounded lists --
  // see js/ui.js's tableState).
  const myItemsSearchInput = document.getElementById('myItemsSearchInput');
  if (myItemsSearchInput) {
    myItemsSearchInput.addEventListener('input', () => setSearch('myItems', myItemsSearchInput.value));
  }
  const myItemsPerPageSelect = document.getElementById('myItemsPerPageSelect');
  if (myItemsPerPageSelect) {
    myItemsPerPageSelect.addEventListener('change', () => setPerPage('myItems', myItemsPerPageSelect.value));
  }

  const quotationCatalogSearchInput = document.getElementById('quotationCatalogSearchInput');
  if (quotationCatalogSearchInput) {
    quotationCatalogSearchInput.addEventListener('input', () => setSearch('quotationCatalog', quotationCatalogSearchInput.value));
  }
  const quotationCatalogPerPageSelect = document.getElementById('quotationCatalogPerPageSelect');
  if (quotationCatalogPerPageSelect) {
    quotationCatalogPerPageSelect.addEventListener('change', () => setPerPage('quotationCatalog', quotationCatalogPerPageSelect.value));
  }

  // Admin/Manager Quotes tab detail modal: "Assign to user" search box --
  // not tied to a table, just a debounced typeahead (see components/
  // quotation.js's searchAssignUsers()).
  const quoteAssignSearchInput = document.getElementById('quoteAssignSearchInput');
  if (quoteAssignSearchInput) {
    quoteAssignSearchInput.addEventListener('input', () => searchAssignUsers(quoteAssignSearchInput.value));
  }

  // Quote Detail modal: "Add another asset" search box (replaces the old
  // long dropdown) -- see components/quotation.js's searchQuoteDetailAssets().
  const quoteDetailAssetSearchInput = document.getElementById('quoteDetailAssetSearchInput');
  if (quoteDetailAssetSearchInput) {
    quoteDetailAssetSearchInput.addEventListener('input', () => searchQuoteDetailAssets(quoteDetailAssetSearchInput.value));
  }

  // My Quote Detail modal (self-service, staff.html/customer.html): its own
  // "Add item" search box -- same pattern as quoteDetailAssetSearchInput
  // above, just scoped to the requester/assignee's own quote.
  const myQuoteDetailAssetSearchInput = document.getElementById('myQuoteDetailAssetSearchInput');
  if (myQuoteDetailAssetSearchInput) {
    myQuoteDetailAssetSearchInput.addEventListener('input', () => searchMyQuoteDetailAssets(myQuoteDetailAssetSearchInput.value));
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
  initThemeToggle();
  // GET /config/public needs no auth, and every page -- including the
  // unauthenticated login page (index.html) -- shows the deployment's
  // brand name in its navbar/login header and <title>, so this runs here
  // unconditionally rather than only inside the `if (session)` block
  // below. See components/quotation.js's loadPublicConfig() /
  // js/ui.js's applySiteName().
  loadPublicConfig();
  // Only does anything on admin.html/manager.html, where the Asset
  // Inventory / User Directory / Ad-Hoc Directory tabs exist -- a no-op
  // (returns immediately) on every other page.
  initSwipeNav();
  // Only does anything on staff.html/customer.html, where the My Items /
  // Equipment Quotation top-level tabs exist -- a no-op everywhere else.
  initDashSwipeNav();
  // App-wide (every page with a modal) -- lets a click on the dimmed
  // backdrop behind any modal close it, same as its Cancel/X button.
  initModalBackdropDismiss();

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

  const editUserForm = document.getElementById('editUserForm');
  if (editUserForm) editUserForm.addEventListener('submit', submitEditUserForm);

  const editOutsiderForm = document.getElementById('editOutsiderForm');
  if (editOutsiderForm) editOutsiderForm.addEventListener('submit', submitEditOutsiderForm);

  const dispatchForm = document.getElementById('dispatchForm');
  if (dispatchForm) dispatchForm.addEventListener('submit', submitDispatchForm);

  const exceptionForm = document.getElementById('exceptionForm');
  if (exceptionForm) exceptionForm.addEventListener('submit', submitExceptionForm);

  const extensionRequestForm = document.getElementById('extensionRequestForm');
  if (extensionRequestForm) extensionRequestForm.addEventListener('submit', submitExtensionRequestForm);

  const directExtendForm = document.getElementById('directExtendForm');
  if (directExtendForm) directExtendForm.addEventListener('submit', submitDirectExtendForm);

  const bulkExtendForm = document.getElementById('bulkExtendForm');
  if (bulkExtendForm) bulkExtendForm.addEventListener('submit', submitBulkExtendForm);

  const denyReasonForm = document.getElementById('denyReasonForm');
  if (denyReasonForm) denyReasonForm.addEventListener('submit', submitDenyReasonForm);

  const vatSettingsForm = document.getElementById('vatSettingsForm');
  if (vatSettingsForm) {
    vatSettingsForm.addEventListener('submit', submitVatSettingsForm);
    loadVatSetting();
  }

  const exportBtn = document.getElementById('exportAuditBtn');
  if (exportBtn) exportBtn.addEventListener('click', () => exportAuditLogs('csv'));

  const exportPdfBtn = document.getElementById('exportAuditPdfBtn');
  if (exportPdfBtn) exportPdfBtn.addEventListener('click', () => exportAuditLogs('pdf'));

  // --- Search boxes / rows-per-page selects on whichever tables exist ---
  wireTableControls();

  // --- Notification Center bell (every dashboard page) ---
  initNotificationBell();

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
      // Personal Notification Center sections (My Overdue/Due Soon/Pending
      // Extension/My Extension Decisions) -- see components/notifications.js.
      // refreshDashboard() already covers this on admin.html/manager.html
      // above; staff.html/customer.html have no dashboard tables to refresh,
      // so trigger it directly here instead.
      refreshNotifications();
    }
    // Self-service Equipment Quotation panel -- staff.html/customer.html
    // only (see components/quotation.js). Independent of the My Items
    // table above, but lives on the same two pages.
    if (document.getElementById('quotationCatalogBody')) {
      initQuotationPage();
    }
    if (document.getElementById('backupTableBody')) {
      refreshBackupsPanel();
    }
    // Admin/Manager "Quotes" tab -- see components/quotation.js. Loads
    // lazily/independently of the Asset Inventory/User Directory tables
    // above, since it lives behind its own tab and doesn't need to block
    // the rest of the dashboard's initial render.
    if (document.getElementById('quotesTableBody')) {
      initQuotesTab();
    }

    // Keep the bell's badge count reasonably fresh even if the person just
    // leaves a dashboard tab open for a while without clicking anything --
    // same "quiet background upkeep" idea as auth.js's idle watchdog, just
    // for notification data instead of session expiry. 60s is frequent
    // enough to feel "live" without hammering the four/five endpoints this
    // fans out to on every tick.
    setInterval(refreshNotifications, 60000);
  }
});
