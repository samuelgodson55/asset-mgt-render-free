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

import { installGlobalErrorBeacon, reportClientError } from './errorbeacon.js';
import { checkAccess, startIdleWatchdog, login, confirmMfaSetup, verifyMfa, redirectByUserRole, logout, getSession, requestPasswordReset, confirmPasswordReset } from './auth.js';
import { qrcode } from './vendor/qrcode.js';
import { closeModal, switchTab, toggleRoute, toggleAdhocExisting, toggleCapacityEdit, toggleNameEdit, toggleCategoryEdit, togglePriceEdit, changePage, setSearch, setPerPage, openRowDetailsFromElement, initSwipeNav, initModalBackdropDismiss, switchDashboardTab, initDashSwipeNav, initSearchClearButtons, downloadTextFile } from './ui.js';
import { toggleTheme, initThemeToggle } from './theme.js';
import { initMaintenanceMode, initMaintenanceControls } from './maintenance.js';
import { refreshDashboard } from './dashboard.js';
import { initNotificationBell, toggleNotificationDropdown, closeNotificationDropdown, refreshNotifications } from './components/notifications.js';

import {
  openDispatchModal, submitDispatchForm, openPropsModal, recallException,
  saveCapacity, saveName, saveCategory, savePrice, submitExceptionForm, submitCreatePoolForm, submitCsvImportForm,
  deleteAssetPool, setAssetsSearch, setAssetsPerPage, changeAssetsPage, openAssetExportModal,
  downloadCsvImportTemplate,
  setDeletedAssetsSearch, setDeletedAssetsPerPage, changeDeletedAssetsPage, restoreAssetPool, purgeAssetPool,
} from './components/assets.js';
import {
  deleteProfile, submitCreateUserForm, setUsersSearch, setUsersPerPage, changeUsersPage,
  openResetPasswordModal, submitResetPasswordForm,
  setDeletedUsersSearch, setDeletedUsersPerPage, changeDeletedUsersPage, restoreUser, purgeUser,
  openEditUserModal, submitEditUserForm,
  openRevokeUserModal, submitRevokeUserForm,
} from './components/users.js';
import { exportAuditLogs, openAuditExportModal, changeAuditPage, setAuditPerPage } from './components/audit.js';
import { loadMyItems } from './components/myitems.js';
import {
  setOutsidersSearch, setOutsidersPerPage, changeOutsidersPage,
  openEditOutsiderModal, submitEditOutsiderForm, deleteOutsider,
  openConvertOutsiderModal, submitConvertOutsiderForm,
} from './components/outsiders.js';
import {
  openCustodyModal, processReturn, updateCustodySelection, toggleSelectAllCustody,
  processAllReturns, bulkProcessReturns, openBulkExtendModal, submitBulkExtendForm,
} from './components/custody.js';
import { openProfileModal, submitChangePasswordForm, ROLE_LABELS, openRegenerateRecoveryCodesModal, submitRegenerateRecoveryCodesForm, downloadRegeneratedRecoveryCodes, closeRecoveryCodesResultModal, submitUpdateIdentityForm } from './components/profile.js';
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
  approveQuote, getCurrentQuoteId, deleteQuoteDetail, deleteQuoteRow,
  openFulfillmentDrawer, updateFulfillmentSelection, toggleSelectAllFulfillment, processFulfillmentSelected,
  addShortfallAllocationRow, removeShortfallAllocationRow,
  openMyQuoteDetail, updateMyQuoteItemQuantity, removeMyQuoteItem,
  addMyQuoteDetailItem, searchMyQuoteDetailAssets, selectMyQuoteDetailAsset, clearMyQuoteDetailAsset,
  toggleQuoteAssignAdhocForm, submitQuoteAssignAdhoc, toggleQuoteAssignAdhocExisting, toggleQuoteAdhocExisting,
} from './components/quotation.js';
import {
  refreshBackupsPanel,
  createBackupNow,
  downloadBackup,
  deleteBackup,
  openRestoreLocalModal,
  openRestoreUploadModal,
  confirmRestore,
  loadDigestRecipients,
  submitDigestRecipientAddForm,
  removeDigestRecipient,
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
  deletedAssets: changeDeletedAssetsPage,
  quotes: changeQuotesPage,
};

// --- Login page 2FA state (index.html only) ---------------------------------
// SECURITY: an account that requires 2FA (currently just super_admin --
// see backend/services/auth_service.py's login()) doesn't get a session
// from the plain email+password form alone -- auth.js's login() returns
// either mfa_setup_required or mfa_required instead of redirecting, and
// the login-form handler below swaps in the matching screen. The
// short-lived setup/pending token that comes back lives ONLY in this
// module-scope variable (never localStorage, never a cookie the backend
// didn't set itself) -- it's gone the moment the tab is closed/refreshed,
// same as it would be if this were a multi-step server-rendered form.
// Module-scope (not inside the DOMContentLoaded closure below) because
// CLICK_ACTIONS' 'cancel-mfa' entry needs to reach cancelMfaFlow() too.
let pendingMfaToken = null;

// Holds the plaintext token read off the URL's ?reset_token=... query
// param (see the DOMContentLoaded handler below) for the lifetime of this
// page load only -- same "never anywhere more persistent than this tab,
// right now" reasoning as pendingMfaToken above. Consumed by
// reset-password-form's submit handler via confirmPasswordReset().
let pendingResetToken = null;

// Set the moment enrollment (or a later regeneration -- see profile.js)
// hands back a fresh recovery-code batch; read by the two click actions
// below and cleared the moment the person moves on.
let pendingRecoveryCodes = null;
let pendingRedirectRole = null;

function showAuthScreen(screenId) {
  [
    'auth-screen', 'mfa-verify-screen', 'mfa-setup-screen', 'mfa-recovery-codes-screen',
    'forgot-password-screen', 'reset-password-screen',
  ].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.classList.toggle('hidden', id !== screenId);
  });
}

// Renders `otpauthUri` (login()'s mfa_setup_required response -- see
// auth.js) as a scannable QR code, using the vendored qrcode-generator
// library (js/vendor/qrcode.mjs -- see that file's header for provenance).
// typeNumber=0 lets the library auto-pick the smallest QR version that
// fits the data; 'M' is standard ~15% error-correction, matching what
// most authenticator apps' own QR codes use. createSvgTag() returns a
// plain, self-contained SVG string built purely from the module grid (no
// user-controlled text is interpolated into it here), so setting it via
// innerHTML is the same trust level as any other markup this app
// generates itself.
function renderMfaSetupQr(otpauthUri) {
  const container = document.getElementById('mfa-setup-qr');
  if (!container) return;
  try {
    const qr = qrcode(0, 'M');
    qr.addData(otpauthUri);
    qr.make();
    container.innerHTML = qr.createSvgTag({ cellSize: 4, margin: 8, scalable: true });
  } catch (e) {
    // Never block enrollment on the QR rendering itself -- the manual-
    // entry key and otpauth URI text below are always shown too, so
    // enrollment still works via either of those if this fails for any
    // reason (e.g. an unexpectedly long otpauth_uri overflowing the
    // library's max QR version).
    container.textContent = 'QR code unavailable -- use the manual entry key below instead.';
  }
}

function showRecoveryCodesScreen(codes, redirectRole) {
  pendingRecoveryCodes = codes;
  pendingRedirectRole = redirectRole;
  const list = document.getElementById('mfa-recovery-codes-list');
  if (list) {
    // Plain text nodes, not innerHTML -- codes are server-generated
    // (see security.py's generate_recovery_code()) from a fixed
    // alphabet, but there's no reason to risk it either way.
    list.replaceChildren(
      ...codes.map((code) => {
        const span = document.createElement('span');
        span.textContent = code;
        return span;
      }),
    );
  }
  showAuthScreen('mfa-recovery-codes-screen');
}

function downloadRecoveryCodes() {
  if (!pendingRecoveryCodes) return;
  const text = [
    'Asset Registry -- 2FA recovery codes',
    'Each code works ONCE. Store this file somewhere safe (a password manager, not your Downloads folder long-term).',
    '',
    ...pendingRecoveryCodes,
    '',
  ].join('\n');
  downloadTextFile('asset-registry-recovery-codes.txt', text);
}

function continuePastRecoveryCodes() {
  const role = pendingRedirectRole;
  pendingRecoveryCodes = null;
  pendingRedirectRole = null;
  redirectByUserRole(role);
}

// Which kind of code #mfa-verify-code currently expects -- 'totp' (default,
// the 6-digit authenticator app code) or 'recovery' (one of the one-time
// XXXXX-XXXXX backup codes from enrollment/regeneration, for when someone
// is locked out of their authenticator app entirely). Both are submitted
// through the exact same field/form/verifyMfa() call -- see auth_service.py's
// mfa_verify(), which tells the two apart itself via
// security.py's is_recovery_code_format() -- this only changes what the
// input *looks like it wants* so recovery codes aren't silently rejected
// by a 6-digit-only pattern/maxlength before they ever reach the backend.
let mfaVerifyMode = 'totp';

// Swaps the #mfa-verify-code input (plus its label/description/toggle-button
// text) between TOTP and recovery-code mode. `focusInput` is false when
// called from cancelMfaFlow() below, since focusing a field on a screen
// that's about to be hidden would just steal focus from the login form.
function setMfaVerifyMode(mode, focusInput = true) {
  mfaVerifyMode = mode;
  const input = document.getElementById('mfa-verify-code');
  const label = document.getElementById('mfa-verify-label');
  const description = document.getElementById('mfa-verify-description');
  const toggle = document.getElementById('mfa-recovery-toggle');
  if (!input) return;
  input.value = '';
  if (mode === 'recovery') {
    input.removeAttribute('pattern');
    input.setAttribute('inputmode', 'text');
    input.setAttribute('autocomplete', 'off');
    input.setAttribute('maxlength', '11'); // "XXXXX-XXXXX"
    input.setAttribute('placeholder', 'XXXXX-XXXXX');
    input.classList.remove('tracking-[0.4em]');
    input.classList.add('tracking-[0.15em]', 'uppercase');
    if (label) label.textContent = 'Recovery code';
    if (description) {
      description.textContent = 'Lost access to your authenticator app? Enter one of the unused recovery codes you saved when you set up 2FA.';
    }
    if (toggle) toggle.textContent = 'Use my authenticator app instead';
  } else {
    input.setAttribute('pattern', '[0-9]*');
    input.setAttribute('inputmode', 'numeric');
    input.setAttribute('autocomplete', 'one-time-code');
    input.setAttribute('maxlength', '6');
    input.setAttribute('placeholder', '123456');
    input.classList.remove('tracking-[0.15em]', 'uppercase');
    input.classList.add('tracking-[0.4em]');
    if (label) label.textContent = 'Authentication code';
    if (description) {
      description.textContent = 'Enter the 6-digit code from your authenticator app to finish signing in.';
    }
    if (toggle) toggle.textContent = 'Use a recovery code instead';
  }
  if (focusInput) input.focus();
}

function toggleMfaVerifyMode() {
  setMfaVerifyMode(mfaVerifyMode === 'totp' ? 'recovery' : 'totp');
}

// #mfa-setup-screen is shown for two different situations that share the
// same fields (fresh secret + QR + confirm form) but need different
// framing: a brand-new account's FIRST-ever enrollment (login()'s
// mfa_setup_required), vs. re-enrolling on a new device after a recovery
// code retired the old secret (mfa_verify()'s mfa_setup_required -- see
// auth_service.py's mfa_verify() docstring). `message` is the backend's
// own wording for the latter case; omit it (or pass nothing) to fall back
// to the generic first-time-setup copy.
const MFA_SETUP_DEFAULT_DESCRIPTION = 'This account requires 2FA. Add it to an authenticator app (Google Authenticator, Authy, 1Password, etc.), then confirm the code it shows below.';
function setMfaSetupDescription(message) {
  const description = document.getElementById('mfa-setup-description');
  if (description) description.textContent = message || MFA_SETUP_DEFAULT_DESCRIPTION;
}

function cancelMfaFlow() {
  pendingMfaToken = null;
  const verifyCode = document.getElementById('mfa-verify-code');
  const setupCode = document.getElementById('mfa-setup-code');
  const qrContainer = document.getElementById('mfa-setup-qr');
  if (verifyCode) verifyCode.value = '';
  if (setupCode) setupCode.value = '';
  if (qrContainer) qrContainer.innerHTML = '';
  // Reset back to the default TOTP look so the next login attempt (this
  // account or another one) always starts from a known state instead of
  // possibly reopening mid-way through a previous recovery-code attempt.
  setMfaVerifyMode('totp', false);
  showAuthScreen('auth-screen');
}

const CLICK_ACTIONS = {
  'switch-tab': (el) => switchTab(el.dataset.tab),
  'switch-dash-tab': (el) => switchDashboardTab(el.dataset.tab),
  'close-modal': (el) => closeModal(el.dataset.modal),
  'toggle-theme': () => toggleTheme(),
  // Login page only -- steps back from either 2FA screen (verify code /
  // enroll) to the plain email+password form, discarding the in-memory
  // mfa_setup_token / mfa_pending_token (see the login-form handler
  // below) since neither is good for anything once abandoned.
  'cancel-mfa': () => cancelMfaFlow(),
  // Login page only -- "Forgot password?" link swaps to the
  // request-a-reset-link screen; its own "Back to sign in" button (and
  // reset-password-screen's) both route back through the same handler.
  'show-forgot-password': () => {
    document.getElementById('forgot-password-message')?.classList.add('hidden');
    document.getElementById('forgot-password-form')?.reset();
    showAuthScreen('forgot-password-screen');
    document.getElementById('forgot-password-identifier')?.focus();
  },
  'cancel-forgot-password': () => showAuthScreen('auth-screen'),
  // Login page 2FA screen only -- flips #mfa-verify-code between expecting
  // a 6-digit authenticator code and an XXXXX-XXXXX recovery code. See
  // setMfaVerifyMode() above.
  'toggle-mfa-recovery-mode': () => toggleMfaVerifyMode(),
  'download-recovery-codes': () => downloadRecoveryCodes(),
  'continue-past-recovery-codes': () => continuePastRecoveryCodes(),
  // "My Profile" -> Two-Factor Authentication (super_admin only -- see
  // profile.js's openProfileModal()) -- regenerate/re-view recovery codes
  // from an already-logged-in session, distinct from the login-page
  // enrollment flow above.
  'open-regenerate-recovery-codes': () => openRegenerateRecoveryCodesModal(),
  'download-regenerated-recovery-codes': () => downloadRegeneratedRecoveryCodes(),
  'close-recovery-codes-result': () => closeRecoveryCodesResultModal(),
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
  'purge-user': (el) => purgeUser(parseInt(el.dataset.userId, 10), el.dataset.userName),
  'edit-user': (el) => openEditUserModal(parseInt(el.dataset.userId, 10)),
  'revoke-user': (el) => openRevokeUserModal(parseInt(el.dataset.userId, 10)),
  'edit-outsider': (el) => openEditOutsiderModal(parseInt(el.dataset.outsiderId, 10)),
  'delete-outsider': (el) => deleteOutsider(parseInt(el.dataset.outsiderId, 10), el.dataset.outsiderName),
  'convert-outsider': (el) => openConvertOutsiderModal(parseInt(el.dataset.outsiderId, 10)),
  'delete-asset-pool': (el) => deleteAssetPool(parseInt(el.dataset.assetId, 10), el.dataset.assetName),
  'restore-asset-pool': (el) => restoreAssetPool(parseInt(el.dataset.assetId, 10), el.dataset.assetName),
  'purge-asset-pool': (el) => purgeAssetPool(parseInt(el.dataset.assetId, 10), el.dataset.assetName),
  'open-asset-export': () => openAssetExportModal(),
  'open-audit-export': () => openAuditExportModal(),
  'export-audit-logs': (el) => exportAuditLogs(el.dataset.format),
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
  'delete-quote-detail': () => deleteQuoteDetail(),
  'delete-quote-row': (el) => deleteQuoteRow(parseInt(el.dataset.quoteId, 10), el.dataset.quoteRef),
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

  // Daily Digest Recipients panel -- see components/backups.js.
  'remove-digest-recipient': (el) => removeDigestRecipient(el.dataset.email),
};

// -----------------------------------------------------------------------------
// DELEGATED "CHANGE" ACTIONS (checkboxes/selects rendered dynamically)
// -----------------------------------------------------------------------------
const CHANGE_ACTIONS = {
  'update-custody-selection': () => updateCustodySelection(),
  'toggle-select-all-custody': (el) => toggleSelectAllCustody(el),
  'toggle-route': () => toggleRoute(),
  'toggle-adhoc-existing': () => toggleAdhocExisting(),
  'toggle-quote-route': () => toggleQuoteRoute(),
  'toggle-quote-adhoc-existing': () => toggleQuoteAdhocExisting(),
  'toggle-quote-assign-adhoc-existing': () => toggleQuoteAssignAdhocExisting(),
  'set-audit-perpage': (el) => setAuditPerPage(el.value),
  'update-order-qty': (el) => updateOrderItemQuantity(parseInt(el.dataset.itemId, 10), el.value),
  'update-my-quote-item-qty': (el) => updateMyQuoteItemQuantity(parseInt(el.dataset.itemId, 10), el.value),
  'update-admin-quote-qty': (el) => updateAdminQuoteItemQuantity(parseInt(el.dataset.itemId, 10), el.value),
  'toggle-fulfillment-selection': () => updateFulfillmentSelection(),
  'toggle-select-all-fulfillment': (el) => toggleSelectAllFulfillment(el),
};

// -----------------------------------------------------------------------------
// FORM SUBMIT GUARD
// -----------------------------------------------------------------------------
// Same "don't let a slow/flaky connection invite multi-click" problem as
// the delegated click-action guard above (see wireDelegatedEvents()), but
// for the app's actual <form> submissions -- Request Extension, Extend,
// Deny Request, Save Changes, Create User, Change Password, CSV Import,
// etc. None of these forms' submit handlers (the `submitXForm(event)`
// functions imported at the top of this file) disabled their own submit
// button, so on a slow connection the button just sat there looking
// clickable while the request was still in flight, and a second (or
// third) tap fired the same request again.
//
// Wraps a form's submit handler so its `[type="submit"]` button is
// disabled the instant the form is submitted and re-enabled the moment
// the handler's promise settles, success or failure -- the exact same
// disable-for-the-duration pattern components/exports.js's
// downloadExport() already uses for every export button. Drop-in
// replacement for `form.addEventListener('submit', handler)`: same two
// arguments, same call sites below, just routed through this guard
// instead of straight to addEventListener.
function wireSubmitGuard(form, handler) {
  if (!form) return;
  form.addEventListener('submit', async (event) => {
    const submitBtn = form.querySelector('[type="submit"]');
    if (submitBtn) submitBtn.disabled = true;
    try {
      await handler(event);
    } finally {
      if (submitBtn) submitBtn.disabled = false;
    }
  });
}

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

    // MULTI-CLICK / SLOW-CONNECTION GUARD: a handful of these handlers
    // (export*, createBackupNow, submitMyQuotation, submitCreateQuote)
    // already disable `el` themselves for the duration of their request --
    // see components/exports.js's module docstring for that pattern. Most
    // of the OTHER data-action handlers wired above (Process Return,
    // Approve/Deny Extension, Restore/Purge/Delete, Save Capacity/Name/
    // Category/Price, Approve Quote, etc.) never did, which meant a slow
    // or flaky connection left the button looking unresponsive right up
    // until the request finally settled -- inviting exactly the
    // "click it again, and again" behavior that then fires the same
    // request multiple times.
    //
    // Rather than repeat the disable/re-enable dance inside every one of
    // those handlers individually, apply it once, generically, right
    // here: if the handler we're about to call is `async` (or otherwise
    // returns a Promise) and `el` is a real button, disable it the moment
    // it's clicked and re-enable it the moment that promise settles,
    // success or failure. Handlers that already manage their own
    // disabled/label state (the ones listed above) just get this as a
    // harmless no-op second layer -- their own `finally` block runs
    // first and already leaves the button re-enabled before this outer
    // `.finally()` ever fires. Handlers that return a plain (non-Promise)
    // value -- e.g. ones that only open a modal -- are untouched.
    const result = action(el);
    if (
      result &&
      typeof result.then === 'function' &&
      (el.tagName === 'BUTTON' || el.tagName === 'INPUT') &&
      !el.disabled
    ) {
      el.disabled = true;
      result.finally(() => {
        el.disabled = false;
      });
    }
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
    { searchId: 'deletedAssetSearchInput', perPageId: 'deletedAssetPerPageSelect', setSearch: setDeletedAssetsSearch, setPerPage: setDeletedAssetsPerPage },
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
// Runs once, before anything else -- if the URL carries ?reset_token=...
// (the person just clicked the link from their "forgot password?" email),
// jump straight to the reset-password screen instead of the normal login
// form/checkAccess() flow. The token is immediately stripped from the
// visible URL via history.replaceState() so it doesn't linger in the
// address bar, browser history, or get re-sent if the tab is reloaded/
// bookmarked -- same "sensitive, single-use, not meant to persist"
// reasoning as pendingResetToken itself.
function checkForPasswordResetLink() {
  const params = new URLSearchParams(window.location.search);
  const token = params.get('reset_token');
  if (!token) return false;
  pendingResetToken = token;
  params.delete('reset_token');
  const cleanedSearch = params.toString();
  const cleanedUrl = window.location.pathname + (cleanedSearch ? `?${cleanedSearch}` : '') + window.location.hash;
  window.history.replaceState({}, document.title, cleanedUrl);
  return true;
}

document.addEventListener('DOMContentLoaded', async () => {
  // Global maintenance gate runs before page-specific data loading.
  if (await initMaintenanceMode()) return;
  initMaintenanceControls();
  if (checkForPasswordResetLink()) {
    showAuthScreen('reset-password-screen');
    document.getElementById('reset-password-new')?.focus();
  }
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

  // --- Login form + 2FA screens (index.html) ---
  // See the module-scope pendingMfaToken/showAuthScreen/cancelMfaFlow
  // declarations near CLICK_ACTIONS above for the shared 2FA state this
  // handler and the 'cancel-mfa' click action both use.
  const loginForm = document.getElementById('login-form');
  if (loginForm) {
    wireSubmitGuard(loginForm, async (e) => {
      e.preventDefault();
      // Data Quality & Usability requirement #6: this single field now
      // accepts EITHER an email address OR a username -- see auth.js's
      // login() and the backend's schemas/auth.py -> LoginRequest.identifier.
      const identifier = document.getElementById('login-email').value;
      const password = document.getElementById('login-password').value;
      try {
        const data = await login(identifier, password);
        if (data.mfa_required) {
          pendingMfaToken = data.mfa_pending_token;
          setMfaVerifyMode('totp', false);
          showAuthScreen('mfa-verify-screen');
          document.getElementById('mfa-verify-code')?.focus();
          return;
        }
        if (data.mfa_setup_required) {
          pendingMfaToken = data.mfa_setup_token;
          document.getElementById('mfa-setup-secret').textContent = data.totp_secret;
          document.getElementById('mfa-setup-uri').textContent = data.otpauth_uri;
          renderMfaSetupQr(data.otpauth_uri);
          setMfaSetupDescription();
          showAuthScreen('mfa-setup-screen');
          document.getElementById('mfa-setup-code')?.focus();
          return;
        }
        redirectByUserRole(data.role);
      } catch (error) {
        alert(error.message);
      }
    });
  }

  const mfaVerifyForm = document.getElementById('mfa-verify-form');
  if (mfaVerifyForm) {
    wireSubmitGuard(mfaVerifyForm, async (e) => {
      e.preventDefault();
      const code = document.getElementById('mfa-verify-code').value;
      try {
        const data = await verifyMfa(pendingMfaToken, code);
        // A recovery code was accepted, but the account's old TOTP
        // secret has now been retired along with it (see
        // auth_service.py's mfa_verify() and auth.js's verifyMfa()) --
        // this device still needs to enroll a fresh one before a real
        // session is granted, exactly like a first-ever login would.
        if (data.mfa_setup_required) {
          pendingMfaToken = data.mfa_setup_token;
          document.getElementById('mfa-setup-secret').textContent = data.totp_secret;
          document.getElementById('mfa-setup-uri').textContent = data.otpauth_uri;
          renderMfaSetupQr(data.otpauth_uri);
          setMfaSetupDescription(data.message);
          showAuthScreen('mfa-setup-screen');
          document.getElementById('mfa-setup-code')?.focus();
          return;
        }
        pendingMfaToken = null;
        redirectByUserRole(data.role);
      } catch (error) {
        alert(error.message);
        document.getElementById('mfa-verify-code').value = '';
        document.getElementById('mfa-verify-code')?.focus();
      }
    });
  }

  const mfaSetupForm = document.getElementById('mfa-setup-form');
  if (mfaSetupForm) {
    wireSubmitGuard(mfaSetupForm, async (e) => {
      e.preventDefault();
      const code = document.getElementById('mfa-setup-code').value;
      try {
        const data = await confirmMfaSetup(pendingMfaToken, code);
        pendingMfaToken = null;
        // recovery_codes is only present on THIS response -- see
        // backend/services/auth_service.py's mfa_setup_confirm() -- show
        // them once before finally continuing to the dashboard.
        if (data.recovery_codes) {
          showRecoveryCodesScreen(data.recovery_codes, data.role);
          return;
        }
        redirectByUserRole(data.role);
      } catch (error) {
        alert(error.message);
        document.getElementById('mfa-setup-code').value = '';
        document.getElementById('mfa-setup-code')?.focus();
      }
    });
  }

  // --- Forgot password (index.html only) ---
  // See auth.js's requestPasswordReset() -- always shows the SAME generic
  // message back, whether or not the identifier matched a real account
  // (backend/services/auth_service.py's request_password_reset() never
  // reveals which). Never treat this as an error state either way -- it
  // always succeeds from the caller's point of view.
  const forgotPasswordForm = document.getElementById('forgot-password-form');
  if (forgotPasswordForm) {
    wireSubmitGuard(forgotPasswordForm, async (e) => {
      e.preventDefault();
      const identifier = document.getElementById('forgot-password-identifier').value;
      const msgEl = document.getElementById('forgot-password-message');
      try {
        const data = await requestPasswordReset(identifier);
        if (msgEl) {
          msgEl.textContent = data.message;
          msgEl.classList.remove('hidden');
        }
        forgotPasswordForm.reset();
      } catch (error) {
        // A genuine network/server error -- NOT "no such account", which
        // never reaches this branch (see requestPasswordReset()'s
        // docstring). Safe to surface as-is.
        if (msgEl) {
          msgEl.textContent = error.message;
          msgEl.classList.remove('hidden');
        }
      }
    });
  }

  // --- Reset password (index.html only, reached via emailed link) ---
  // `pendingResetToken` is read off the URL's ?reset_token= query param
  // once, below (right after this form-wiring block) -- see auth.js's
  // confirmPasswordReset() for how it's actually redeemed.
  //
  // NOTE: named forgotPasswordResetForm (not resetPasswordForm) even
  // though its own element id IS "reset-password-form" -- there's already
  // an UNRELATED `resetPasswordForm` further down in this same
  // DOMContentLoaded scope, bound to the ADMIN "reset a user's password"
  // modal (`#resetPasswordForm`, backend's UserPasswordResetRequest
  // flow). Two `const`s with the same name in the same scope is a syntax
  // error terser (the frontend build's minifier) rejects outright at
  // build time -- see build-frontend/build.js -- so these two,
  // similarly-named-but-unrelated forms need visibly different variable
  // names here even though their DOM ids don't collide.
  const forgotPasswordResetForm = document.getElementById('reset-password-form');
  if (forgotPasswordResetForm) {
    wireSubmitGuard(forgotPasswordResetForm, async (e) => {
      e.preventDefault();
      const newPassword = document.getElementById('reset-password-new').value;
      const confirmPassword = document.getElementById('reset-password-confirm').value;
      const msgEl = document.getElementById('reset-password-message');
      const setMsg = (text, isError) => {
        if (!msgEl) return;
        msgEl.textContent = text;
        msgEl.classList.remove('hidden');
        msgEl.classList.toggle('text-rose-400', isError);
        msgEl.classList.toggle('text-emerald-400', !isError);
      };
      if (newPassword !== confirmPassword) {
        setMsg('New password and confirmation do not match.', true);
        return;
      }
      if (!pendingResetToken) {
        setMsg('This password reset link is invalid or has expired. Request a new one.', true);
        return;
      }
      try {
        const data = await confirmPasswordReset(pendingResetToken, newPassword);
        setMsg(data.message, false);
        pendingResetToken = null;
        forgotPasswordResetForm.reset();
      } catch (error) {
        setMsg(error.message, true);
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
  if (createPoolForm) wireSubmitGuard(createPoolForm, submitCreatePoolForm);

  const csvImportForm = document.getElementById('csvImportForm');
  if (csvImportForm) wireSubmitGuard(csvImportForm, submitCsvImportForm);

  // The CSV file input auto-submits its form the moment a file is chosen,
  // rather than requiring a separate "Upload" click.
  const csvFileInput = document.getElementById('csvFileInput');
  if (csvFileInput && csvImportForm) {
    csvFileInput.addEventListener('change', () => csvImportForm.requestSubmit());
  }
  wireCsvDragAndDrop(csvFileInput, csvImportForm);

  const createUserForm = document.getElementById('createUserForm');
  if (createUserForm) wireSubmitGuard(createUserForm, submitCreateUserForm);

  const changePasswordForm = document.getElementById('changePasswordForm');
  if (changePasswordForm) wireSubmitGuard(changePasswordForm, submitChangePasswordForm);
  const updateIdentityForm = document.getElementById('updateIdentityForm');
  if (updateIdentityForm) wireSubmitGuard(updateIdentityForm, submitUpdateIdentityForm);

  const regenerateRecoveryCodesForm = document.getElementById('regenerateRecoveryCodesForm');
  if (regenerateRecoveryCodesForm) wireSubmitGuard(regenerateRecoveryCodesForm, submitRegenerateRecoveryCodesForm);

  const resetPasswordForm = document.getElementById('resetPasswordForm');
  if (resetPasswordForm) wireSubmitGuard(resetPasswordForm, submitResetPasswordForm);

  const editUserForm = document.getElementById('editUserForm');
  if (editUserForm) wireSubmitGuard(editUserForm, submitEditUserForm);
  const revokeUserForm = document.getElementById('revokeUserForm');
  if (revokeUserForm) wireSubmitGuard(revokeUserForm, submitRevokeUserForm);

  const editOutsiderForm = document.getElementById('editOutsiderForm');
  if (editOutsiderForm) wireSubmitGuard(editOutsiderForm, submitEditOutsiderForm);
  const convertOutsiderForm = document.getElementById('convertOutsiderForm');
  if (convertOutsiderForm) wireSubmitGuard(convertOutsiderForm, submitConvertOutsiderForm);

  const dispatchForm = document.getElementById('dispatchForm');
  if (dispatchForm) wireSubmitGuard(dispatchForm, submitDispatchForm);

  const exceptionForm = document.getElementById('exceptionForm');
  if (exceptionForm) wireSubmitGuard(exceptionForm, submitExceptionForm);

  const extensionRequestForm = document.getElementById('extensionRequestForm');
  if (extensionRequestForm) wireSubmitGuard(extensionRequestForm, submitExtensionRequestForm);

  const directExtendForm = document.getElementById('directExtendForm');
  if (directExtendForm) wireSubmitGuard(directExtendForm, submitDirectExtendForm);

  const bulkExtendForm = document.getElementById('bulkExtendForm');
  if (bulkExtendForm) wireSubmitGuard(bulkExtendForm, submitBulkExtendForm);

  const denyReasonForm = document.getElementById('denyReasonForm');
  if (denyReasonForm) wireSubmitGuard(denyReasonForm, submitDenyReasonForm);

  const vatSettingsForm = document.getElementById('vatSettingsForm');
  if (vatSettingsForm) {
    wireSubmitGuard(vatSettingsForm, submitVatSettingsForm);
    loadVatSetting();
  }

  const digestRecipientAddForm = document.getElementById('digestRecipientAddForm');
  if (digestRecipientAddForm) {
    wireSubmitGuard(digestRecipientAddForm, submitDigestRecipientAddForm);
    loadDigestRecipients();
  }

  // --- Search boxes / rows-per-page selects on whichever tables exist ---
  wireTableControls();

  // --- "x" clear button on every search box (see ui.js's
  // initSearchClearButtons() docstring) ---
  initSearchClearButtons();

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
      // Super Admin ONLY (see admin.html's #systemBackupsSection comment
      // and backend/deps.require_true_super_admin) -- a plain `admin`
      // session gets the whole panel hidden rather than a disabled/greyed
      // version of it, since `admin` has zero access to any of
      // /backup/*, not just Restore.
      const backupsSection = document.getElementById('systemBackupsSection');
      if (session.role === 'super_admin') {
        refreshBackupsPanel();
      } else if (backupsSection) {
        backupsSection.remove();
      }
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


// Capture browser/runtime failures that never reach a component catch block.
installGlobalErrorBeacon();
