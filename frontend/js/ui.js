// =============================================================================
// js/ui.js
// -----------------------------------------------------------------------------
// Generic, page-agnostic UI helpers shared by every component: modal
// open/close, HTML-escaping, tab switching, the dispatch-drawer route
// toggle, the Properties Hub capacity-edit toggle, and the generic
// search/pagination engine reused by every listing table.
//
// Nothing in this file knows about assets/users/outsiders specifically --
// `components/*.js` call into this file, not the other way around.
// =============================================================================

// Every modal in this app ("Issue/Dispatch" drawer, "Custody Ledger"
// drawer, "My Profile" window) is built the same way: a `fixed inset-0 ...
// hidden items-center justify-center` (or `justify-end`, for the
// slide-in drawers) wrapper div around the actual modal box, so the box
// ends up centered (or docked to the right edge) via Flexbox.
//
// BUG THIS FIXES: `items-center`/`justify-center`/`justify-end` only do
// anything on an element that is ALSO `display: flex` (or `grid`) -- and
// this function used to only ever remove the `hidden` class, never add a
// `flex` class back. That meant every modal's wrapper was rendering as a
// plain `display: block` div the whole time: the alignment utilities were
// silently no-ops, so the modal box itself was left sitting in its default
// block-flow position (pinned to the top-left, ignoring `items-center`/
// `justify-center`) instead of actually being centered/docked on screen --
// this is what the "user properties window" (My Profile) alignment
// problem was.
//
// The fix: toggle `hidden` and `flex` TOGETHER, as a pair, exactly like
// `toggleCapacityEdit()` below already does for the same reason. (We
// deliberately do NOT just leave a static `flex` class sitting on the
// element next to `hidden` in the HTML -- Tailwind explicitly warns against
// that combination, since which one "wins" depends on the order its
// utilities happen to be generated in, which isn't something you want to
// depend on. Toggling them from JS removes that ambiguity entirely.)
export function openModal(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.remove('hidden');
  el.classList.add('flex');
}

export function closeModal(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.add('hidden');
  el.classList.remove('flex');
}

// -----------------------------------------------------------------------------
// CLICK-OUTSIDE-TO-CLOSE (every modal)
// -----------------------------------------------------------------------------
// Every modal in this app is built the same way (see openModal()'s own
// comment above): a `fixed inset-0 ... [id$="Modal"]` wrapper containing a
// `.backdrop` div (the dim/blurred layer) as one sibling and the actual
// visible panel as another. Clicking the backdrop itself -- i.e. anywhere
// in the dimmed area OUTSIDE the panel -- should dismiss the modal, same
// as tapping its Cancel/X button.
//
// This works for every current and future modal with zero per-modal
// wiring: it's one delegated listener here, gated on the click TARGET
// itself being the `.backdrop` element (not something that merely
// bubbled through it, which can't happen anyway since the panel is a
// sibling of `.backdrop`, not a descendant of it -- but the explicit
// check keeps this robust even if that markup pattern ever changes).
// Every close button in this app already just closes with no
// confirmation step first (see admin.html's `data-action="close-modal"`
// buttons), so dismissing this same way from the backdrop is safe too.
export function initModalBackdropDismiss() {
  document.addEventListener('click', (event) => {
    if (!event.target.classList.contains('backdrop')) return;
    const modal = event.target.closest('[id$="Modal"]');
    if (modal) closeModal(modal.id);
  });
}

// =============================================================================
// TRANSIENT TOAST (start of moving this app off browser alert())
// -----------------------------------------------------------------------------
// A small, self-contained "toast" notification -- built and appended to
// <body> on first use, so no per-page HTML markup is needed anywhere. Used
// right now for the one thing every export/download button in this app
// used to finish completely silently (no alert(), no confirmation of any
// kind): components/exports.js's downloadExport() and
// components/backups.js's downloadBackup() both call this once the file
// has actually been handed to the browser.
//
// Deliberately NOT used yet for error paths -- this is a gradual move off
// alert(), not a rewrite of every alert() in one pass (see the
// showFieldError()/clearFieldError() pair further down for the other half
// of that effort, covering form validation instead of confirmations).
const TOAST_AUTO_DISMISS_MS = 2500;
let toastContainer = null;

function getToastContainer() {
  if (toastContainer && document.body.contains(toastContainer)) return toastContainer;
  toastContainer = document.createElement('div');
  toastContainer.id = 'toastContainer';
  // Stacked bottom-right, above everything (including modals, since a
  // download can be triggered from inside one -- e.g. the Custody Ledger
  // drawer's export buttons).
  toastContainer.className = 'fixed bottom-4 right-4 z-[100] flex flex-col items-end gap-2';
  document.body.appendChild(toastContainer);
  return toastContainer;
}

// `message` should stay short -- this is a passing confirmation, not a
// place to explain anything (that's what the export/download itself, or an
// error alert(), is for).
export function showToast(message) {
  const container = getToastContainer();
  const toast = document.createElement('div');
  toast.className = 'flex items-center gap-2 rounded-lg border border-emerald-500/30 bg-[#141922] px-4 py-2.5 text-[13px] font-medium text-slate-200 shadow-2xl opacity-0 translate-y-1 transition-all duration-200';
  toast.innerHTML = `
    <svg class="h-4 w-4 shrink-0 text-emerald-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/></svg>
    <span></span>
  `;
  toast.querySelector('span').textContent = message;
  container.appendChild(toast);

  // Two rAFs (not one) so the browser has definitely committed the
  // "opacity-0 translate-y-1" starting state to the layout before we
  // remove it -- otherwise the transition can get coalesced away and the
  // toast just pops in with no fade, since the class add/remove would
  // happen inside the same paint frame.
  requestAnimationFrame(() => requestAnimationFrame(() => {
    toast.classList.remove('opacity-0', 'translate-y-1');
  }));

  setTimeout(() => {
    toast.classList.add('opacity-0', 'translate-y-1');
    setTimeout(() => toast.remove(), 200); // let the fade-out finish before removing
  }, TOAST_AUTO_DISMISS_MS);
}

// =============================================================================
// INLINE FIELD VALIDATION (start of moving simple "you missed a field"
// checks off browser alert(), same spirit as showToast() above)
// -----------------------------------------------------------------------------
// Every one of this app's forms already gives each input a stable `id`
// (that's how e.g. saveName()/processReturn()/submitExtensionRequestForm()
// read the value out in the first place) -- so rather than a new markup
// convention, showFieldError() just finds the input by that same id and
// inserts one small error message right after it, creating that message
// element the first time and reusing/updating it on every call after (so
// re-submitting with the same mistake doesn't stack up duplicate error
// lines under the field).
//
// Deliberately scoped to ONE field at a time, not a whole-form
// summary -- every current caller only ever has exactly one thing wrong at
// a time (an empty required field), so there's nothing to aggregate yet.
// `floating`: pass true for an input that lives in a `flex`/`grid` ROW
// alongside other controls (e.g. custody.js's Return Quantity field, which
// sits next to the Extend/Process Return buttons) -- the error message is
// then positioned absolutely below just the input instead of becoming its
// own flex/grid item, which would otherwise wedge a wide error message in
// between the narrow input and whatever comes after it. Every current
// caller with a normal block-stacked form (Asset Name, the two Extension
// modals) leaves this false, letting the message push layout below the
// field the same way a native validation message would.
export function showFieldError(inputId, message, floating = false) {
  const input = document.getElementById(inputId);
  if (!input) return;

  input.classList.add('border-rose-500', 'focus:border-rose-500');
  // Only meaningful on a border that was already `border-blue-500/60` on
  // focus -- harmless no-op to remove on inputs that don't have it.
  input.classList.remove('focus:border-blue-500/60', 'focus:border-emerald-500/60');

  const errorId = `${inputId}-error`;
  let errorEl = document.getElementById(errorId);
  if (!errorEl) {
    errorEl = document.createElement('p');
    errorEl.id = errorId;
    errorEl.className = floating
      ? 'absolute left-0 top-full mt-1 whitespace-nowrap text-[11px] font-medium text-rose-400'
      : 'mt-1 text-[11px] font-medium text-rose-400';
    input.insertAdjacentElement('afterend', errorEl);
  }
  errorEl.textContent = message;

  // Clearing on the field's very next edit (rather than requiring another
  // failed submit, or leaving the error up until the form is resubmitted
  // successfully) is what makes this feel like real-time validation
  // instead of just a relocated alert() -- `{ once: true }` means this
  // never needs its own removeEventListener bookkeeping.
  input.addEventListener('input', () => clearFieldError(inputId), { once: true });
}

export function clearFieldError(inputId) {
  const input = document.getElementById(inputId);
  if (input) {
    input.classList.remove('border-rose-500', 'focus:border-rose-500');
  }
  const errorEl = document.getElementById(`${inputId}-error`);
  if (errorEl) errorEl.remove();
}

export function escapeHtml(str) {
  if (str === null || str === undefined) return '';
  return String(str).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// -----------------------------------------------------------------------------
// MOBILE "ROW DETAILS" POPUP
// -----------------------------------------------------------------------------
// Every data table in this app hides its less-critical columns below the
// `sm` breakpoint (480px -- see build-tailwind/tailwind.config.js's
// `screens.sm` for the single source of truth on this number, and how to
// change it) -- see each components/*.js render function's `hidden
// sm:table-cell` cells -- so a table never forces the page into a long
// horizontal scroll on a phone. Desktop (`sm:` and up) still renders
// every column exactly as before.
//
// Whatever gets hidden isn't lost, though: a small "Details" button (also
// only shown below `sm`, via `sm:hidden`) opens THIS one shared popup with
// the hidden fields as a simple label/value list. Every table on every
// page reuses the SAME modal markup (`id="rowDetailsModal"`, added once
// near the bottom of each dashboard HTML file) -- a render function just
// calls `rowDetailsTrigger(title, fields)` to build its mobile button's
// `data-*` attributes. See components/assets.js's renderAssetsTable() for
// the first example.
//
// `fields` is an array of `[label, value]` pairs, e.g.:
//   [['Available / Total', '4 / 10 units'], ['Pool ID', 'POOL-7']]
// `value` may contain HTML (e.g. a status-badge `<span>`) -- same
// discipline as everywhere else in this codebase: the CALLER is
// responsible for escapeHtml()-ing any raw/untrusted text before putting
// it in the array, since openRowDetailsFromElement() below renders it via
// innerHTML, not textContent.
export function rowDetailsTrigger(title, fields) {
  // JSON-encode the fields, then HTML-escape the whole blob so it's safe
  // to sit inside a double-quoted HTML attribute -- the browser reverses
  // that escaping automatically when JS later reads `el.dataset.fields`,
  // so openRowDetailsFromElement() just JSON.parses it straight back out.
  return `data-action="open-row-details" data-title="${escapeHtml(title)}" data-fields='${escapeHtml(JSON.stringify(fields))}'`;
}

// Called by main.js's delegated click handler for any element carrying
// `data-action="open-row-details"` (built by rowDetailsTrigger() above).
export function openRowDetailsFromElement(el) {
  const titleEl = document.getElementById('rowDetailsTitle');
  const bodyEl = document.getElementById('rowDetailsBody');
  if (!titleEl || !bodyEl) return; // shouldn't happen -- every dashboard HTML file includes the shared #rowDetailsModal

  titleEl.textContent = el.dataset.title || 'Details';

  let fields = [];
  try {
    fields = JSON.parse(el.dataset.fields || '[]');
  } catch {
    fields = [];
  }

  // A field with an empty-string label is treated as a full-width block
  // instead of a label/value row -- used for the "Actions" row each table
  // now appends (see components/assets.js, users.js, outsiders.js,
  // myitems.js) so the same Issue/Dispatch, Properties Hub, Delete, etc.
  // buttons that desktop shows inline in the table row are still reachable
  // on mobile once that row's actions column is hidden below `sm`. Those
  // buttons carry their own real `data-action` attributes, so main.js's
  // delegated body-level click handler wires them up automatically no
  // matter where in the DOM they end up.
  bodyEl.innerHTML = fields.map(([label, value]) => label
    ? `<div class="flex items-start justify-between gap-4 border-b border-border/60 py-2.5 last:border-0">
      <dt class="text-slate-500">${escapeHtml(label)}</dt>
      <dd class="text-right font-medium text-slate-200">${value}</dd>
    </div>`
    : `<div class="pt-3">${value}</div>`
  ).join('') || `<p class="py-2 text-center text-slate-500">No additional details.</p>`;

  openModal('rowDetailsModal');
}

// Turns a backend ISO-8601 timestamp (e.g.
// "2026-07-08T07:58:40.216313+00:00") into a short, human-friendly string
// for on-screen tables (e.g. "Jul 8, 2026, 7:58 AM") -- the raw ISO string
// (microseconds + numeric UTC offset included) is what the API returns and
// is fine for the CSV/PDF exports, but it's unreadable/overflow-prone in
// the UI table, which only has room for a glance-able value. Falls back to
// the original string if it's not a value `Date` can parse, so a malformed
// or already-friendly timestamp still renders as *something* instead of
// blank.
export function formatTimestamp(isoString) {
  if (!isoString) return '';
  const d = new Date(isoString);
  if (Number.isNaN(d.getTime())) return isoString;
  // Fixed "YYYY-MM-DD HH:MM:SS" (24-hour, local time) instead of a
  // locale-dependent string -- consistent, sortable-looking, and doesn't
  // vary in width from one row to the next (which is what made table
  // columns using it jump around before).
  const pad = (n) => String(n).padStart(2, '0');
  const yyyy = d.getFullYear();
  const mm = pad(d.getMonth() + 1);
  const dd = pad(d.getDate());
  const hh = pad(d.getHours());
  const min = pad(d.getMinutes());
  const ss = pad(d.getSeconds());
  return `${yyyy}-${mm}-${dd} ${hh}:${min}:${ss}`;
}

export function switchTab(tab) {
  const assets = document.getElementById('assetInventorySection');
  const users = document.getElementById('userDirectorySection');
  const adhoc = document.getElementById('adhocDirectorySection');
  const quotes = document.getElementById('quotesSection');
  const logs = document.getElementById('auditBackupsSection');
  const tabAssets = document.getElementById('tabAssets');
  const tabUsers = document.getElementById('tabUsers');
  const tabAdhoc = document.getElementById('tabAdhoc');
  const tabQuotes = document.getElementById('tabQuotes');
  const tabLogs = document.getElementById('tabLogs');
  if (!assets || !users) return;

  const activeCls = ['border-blue-500', 'text-slate-50', 'font-semibold'];
  const inactiveCls = ['border-transparent', 'text-slate-500', 'font-medium'];
  const allTabs = [tabAssets, tabUsers, tabAdhoc, tabQuotes, tabLogs].filter(Boolean);
  const allSections = [assets, users, adhoc, quotes, logs].filter(Boolean);

  allSections.forEach(s => s.classList.add('hidden'));
  allTabs.forEach(t => { t.classList.add(...inactiveCls); t.classList.remove(...activeCls); });

  let activeSection;
  if (tab === 'assets') {
    activeSection = assets;
    assets.classList.remove('hidden');
    tabAssets.classList.add(...activeCls); tabAssets.classList.remove(...inactiveCls);
  } else if (tab === 'adhoc' && adhoc) {
    activeSection = adhoc;
    adhoc.classList.remove('hidden');
    tabAdhoc.classList.add(...activeCls); tabAdhoc.classList.remove(...inactiveCls);
  } else if (tab === 'quotes' && quotes) {
    activeSection = quotes;
    quotes.classList.remove('hidden');
    tabQuotes.classList.add(...activeCls); tabQuotes.classList.remove(...inactiveCls);
  } else if (tab === 'logs' && logs) {
    activeSection = logs;
    logs.classList.remove('hidden');
    tabLogs.classList.add(...activeCls); tabLogs.classList.remove(...inactiveCls);
  } else {
    activeSection = users;
    users.classList.remove('hidden');
    tabUsers.classList.add(...activeCls); tabUsers.classList.remove(...inactiveCls);
  }

  updateSwipeDots(tab);

  // Small fade+slide-in on the freshly-revealed section, mostly noticeable
  // when this was triggered by a swipe gesture (initSwipeNav() below) --
  // re-trigger by removing/re-adding the class in case it's still present
  // from a previous switch (a class added twice in a row without ever
  // being removed wouldn't re-play the CSS animation).
  if (activeSection) {
    activeSection.classList.remove('swipe-content-enter');
    // eslint-disable-next-line no-unused-expressions
    void activeSection.offsetWidth; // force reflow so the class removal above "takes" before re-adding it
    activeSection.classList.add('swipe-content-enter');
  }
}

// -----------------------------------------------------------------------------
// SWIPE-BETWEEN-TABS (mobile)
// -----------------------------------------------------------------------------
// admin.html / manager.html are the only pages with this tabbed nav (see
// switchTab() above) -- both have 5 tabs (Asset Inventory / User
// Directory / Ad-Hoc Directory / Quotes / Audit Logs). manager.html's
// #auditBackupsSection only contains the Audit Trail table -- no System
// Backups panel next to it, since backups stay Super Admin/Admin-only
// (see admin.html's own comment on #auditBackupsSection). It reuses the
// same section id as admin.html purely so this shared switchTab()/
// getSwipeTabOrder() logic below works on both pages unmodified.
// getSwipeTabOrder()'s `document.getElementById` filter below means both
// pages "just work" here without any per-page branching; on every other
// page the `#tabAssets` lookup fails fast and both functions are no-ops.
//
// The order swiping moves through always matches left-to-right reading
// order of the tabs actually present on THIS page/role (Ad-Hoc doesn't
// exist for every role), rather than a hardcoded list, so it can't ever
// try to switch to a tab that isn't there.
function getSwipeTabOrder() {
  return ['assets', 'users', 'adhoc', 'quotes', 'logs'].filter(t => document.getElementById(`tab${t[0].toUpperCase()}${t.slice(1)}`));
}

function getActiveSwipeTab(order) {
  const sectionIds = { assets: 'assetInventorySection', users: 'userDirectorySection', adhoc: 'adhocDirectorySection', quotes: 'quotesSection', logs: 'auditBackupsSection' };
  return order.find(t => {
    const el = document.getElementById(sectionIds[t]);
    return el && !el.classList.contains('hidden');
  }) || order[0];
}

// A thin strip of small dots (one per tab) shown only below the `sm`
// breakpoint -- see the `<div id="swipeDots">` markup added right under
// the tab content in admin.html/manager.html -- so a phone visitor has a
// visual hint that this area is swipeable, and can see at a glance which
// of the 2-3 sections they're currently on. Desktop already has the full
// text tab labels for that, so this stays hidden there.
function updateSwipeDots(activeTab) {
  const strip = document.getElementById('swipeDots');
  if (!strip) return;
  const order = getSwipeTabOrder();
  strip.innerHTML = order.map(t =>
    `<span class="swipe-dot${t === activeTab ? ' is-active' : ''}"></span>`
  ).join('');
}

// Attaches a single touchstart/touchend pair to the whole scrollable tab
// -content area. Deliberately conservative about when it actually counts
// as a "swipe" (rather than a vertical scroll or a tap): the horizontal
// distance has to clear a real threshold AND clearly dominate over
// whatever vertical movement also happened, and it's ignored entirely
// while any modal is open (so swiping to dismiss/scroll inside a modal --
// e.g. the Properties Hub sheet -- never accidentally also changes the
// tab underneath it).
export function initSwipeNav() {
  const order = getSwipeTabOrder();
  if (order.length < 2) return; // nothing to swipe between on this page/role

  const swipeArea = document.getElementById('swipeArea');
  if (!swipeArea) return;

  updateSwipeDots(getActiveSwipeTab(order));

  const H_THRESHOLD = 60; // minimum horizontal travel, in px, to count as a swipe
  let startX = 0, startY = 0, tracking = false;

  swipeArea.addEventListener('touchstart', (event) => {
    if (event.touches.length !== 1) { tracking = false; return; }
    // Any open modal "wins" -- don't also change tabs underneath it.
    if (document.querySelector('.fixed.flex[id$="Modal"]')) { tracking = false; return; }
    startX = event.touches[0].clientX;
    startY = event.touches[0].clientY;
    tracking = true;
  }, { passive: true });

  swipeArea.addEventListener('touchend', (event) => {
    if (!tracking) return;
    tracking = false;
    const dx = event.changedTouches[0].clientX - startX;
    const dy = event.changedTouches[0].clientY - startY;
    if (Math.abs(dx) < H_THRESHOLD || Math.abs(dx) < Math.abs(dy) * 1.3) return; // too vertical/short -- treat as a scroll, not a swipe

    const currentOrder = getSwipeTabOrder(); // re-read in case the role's tabs changed since mount (they don't, but cheap insurance)
    const activeTab = getActiveSwipeTab(currentOrder);
    const currentIndex = currentOrder.indexOf(activeTab);
    // Swiping LEFT (dx negative) advances to the NEXT tab, mirroring how a
    // person swipes left to go "forward" through a carousel/photo album;
    // swiping RIGHT goes back.
    const nextIndex = dx < 0 ? currentIndex + 1 : currentIndex - 1;
    if (nextIndex < 0 || nextIndex >= currentOrder.length) return; // already at an edge -- nothing to do
    switchTab(currentOrder[nextIndex]);
  }, { passive: true });
}

// -----------------------------------------------------------------------------
// TOP-LEVEL "My Items" / "Equipment Quotation" TABS (staff.html/customer.html)
// -----------------------------------------------------------------------------
// Same visual language and swipe-gesture pattern as switchTab()/initSwipeNav()
// above (admin.html/manager.html) and components/quotation.js's own
// switchQuotationTab() (the Order/History pair *inside* the Equipment
// Quotation card) -- this is a third, independent pair scoped to the two
// top-level sections on the customer/staff dashboards: #dashItemsSection
// ("My Checked-Out Items") and #dashQuotationSection ("Equipment
// Quotation", which itself has its own Order/History tabs nested inside).
// Kept separate from switchTab() rather than generalizing it further since
// admin/manager's tab set and this one never appear on the same page.
// =============================================================================
const DASH_TAB_ORDER = ['items', 'quotation'];

export function switchDashboardTab(tab) {
  const itemsSection = document.getElementById('dashItemsSection');
  const quotationSection = document.getElementById('dashQuotationSection');
  const tabItems = document.getElementById('dashTabItems');
  const tabQuotation = document.getElementById('dashTabQuotation');
  if (!itemsSection || !quotationSection) return;

  const activeCls = ['border-blue-500', 'text-slate-50', 'font-semibold'];
  const inactiveCls = ['border-transparent', 'text-slate-500', 'font-medium'];
  const isItems = tab !== 'quotation';

  itemsSection.classList.toggle('hidden', !isItems);
  quotationSection.classList.toggle('hidden', isItems);
  if (tabItems) { tabItems.classList.remove(...activeCls, ...inactiveCls); tabItems.classList.add(...(isItems ? activeCls : inactiveCls)); }
  if (tabQuotation) { tabQuotation.classList.remove(...activeCls, ...inactiveCls); tabQuotation.classList.add(...(isItems ? inactiveCls : activeCls)); }

  updateDashSwipeDots(isItems ? 'items' : 'quotation');

  const activeSection = isItems ? itemsSection : quotationSection;
  activeSection.classList.remove('swipe-content-enter');
  void activeSection.offsetWidth; // force reflow so the CSS animation re-plays
  activeSection.classList.add('swipe-content-enter');
}

function updateDashSwipeDots(activeTab) {
  const strip = document.getElementById('dashSwipeDots');
  if (!strip) return;
  strip.innerHTML = DASH_TAB_ORDER.map(t =>
    `<span class="swipe-dot${t === activeTab ? ' is-active' : ''}"></span>`
  ).join('');
}

// Same conservative swipe heuristic as initSwipeNav()/initQuotationSwipeNav()
// above -- a no-op on any page without #dashSwipeArea (i.e. every page
// except staff.html/customer.html).
export function initDashSwipeNav() {
  const swipeArea = document.getElementById('dashSwipeArea');
  if (!swipeArea) return;
  updateDashSwipeDots('items');

  const H_THRESHOLD = 60;
  let startX = 0, startY = 0, tracking = false;

  swipeArea.addEventListener('touchstart', (event) => {
    if (event.touches.length !== 1) { tracking = false; return; }
    if (document.querySelector('.fixed.flex[id$="Modal"]')) { tracking = false; return; }
    startX = event.touches[0].clientX;
    startY = event.touches[0].clientY;
    tracking = true;
  }, { passive: true });

  swipeArea.addEventListener('touchend', (event) => {
    if (!tracking) return;
    tracking = false;
    const dx = event.changedTouches[0].clientX - startX;
    const dy = event.changedTouches[0].clientY - startY;
    if (Math.abs(dx) < H_THRESHOLD || Math.abs(dx) < Math.abs(dy) * 1.3) return;

    const quotationSection = document.getElementById('dashQuotationSection');
    const currentlyOnItems = quotationSection ? quotationSection.classList.contains('hidden') : true;
    if (dx < 0 && currentlyOnItems) switchDashboardTab('quotation'); // swipe left -> next tab
    else if (dx > 0 && !currentlyOnItems) switchDashboardTab('items'); // swipe right -> previous tab
  }, { passive: true });
}

export function toggleRoute() {
  const val = document.getElementById('routeSelect').value;
  document.getElementById('staffField').classList.toggle('hidden', val !== 'staff');
  document.getElementById('customerField').classList.toggle('hidden', val !== 'customer');
  document.getElementById('adhocField').classList.toggle('hidden', val !== 'adhoc');
  toggleAdhocExisting();
}

// Sub-toggle inside the "Ad-Hoc Individual" route (see toggleRoute() above):
// #adhocExistingSelect offers a choice between every ad-hoc profile
// already on file (populated by components/outsiders.js's
// populateOutsiderSelects()) and a leading "+ Create New Unlinked
// Profile" option. The Unlinked Profile Details mini-form
// (#adhocNewFields) only needs to be shown -- and only needs its
// name/contact filled in -- when that "create new" option is selected;
// picking an existing profile just needs its id (see components/
// assets.js's submitDispatchForm()).
export function toggleAdhocExisting() {
  const select = document.getElementById('adhocExistingSelect');
  const newFields = document.getElementById('adhocNewFields');
  if (!select || !newFields) return;
  newFields.classList.toggle('hidden', select.value !== 'new');
}

export function toggleCapacityEdit() {
  document.getElementById('capacityBtn').classList.toggle('hidden');
  const edit = document.getElementById('capacityEdit');
  edit.classList.toggle('hidden');
  edit.classList.toggle('flex');
}

export function toggleNameEdit() {
  document.getElementById('nameDisplay').classList.toggle('hidden');
  const edit = document.getElementById('nameEdit');
  edit.classList.toggle('hidden');
  edit.classList.toggle('flex');
  // Whichever direction this toggle just went (opening fresh or cancelling
  // out), any leftover "Asset name cannot be empty." from a previous
  // attempt shouldn't still be showing -- see saveName()'s
  // showFieldError('nameInput', ...) in components/assets.js.
  clearFieldError('nameInput');
}

// Same show/hide toggle as toggleNameEdit() above, for the Properties
// Hub's category field (admin.html only -- see components/assets.js's
// saveCategory()/openPropsModal()).
export function toggleCategoryEdit() {
  document.getElementById('categoryDisplay').classList.toggle('hidden');
  const edit = document.getElementById('categoryEdit');
  edit.classList.toggle('hidden');
  edit.classList.toggle('flex');
}

// Same show/hide toggle as toggleCategoryEdit() above, for the
// Properties Hub's price field (admin.html only -- see components/
// assets.js's savePrice()/openPropsModal()).
export function togglePriceEdit() {
  document.getElementById('priceDisplay').classList.toggle('hidden');
  const edit = document.getElementById('priceEdit');
  edit.classList.toggle('hidden');
  edit.classList.toggle('flex');
}

// Formats a numeric price (or null/undefined) as a "₦1,899.00"-style
// string for display -- shared by the Asset Inventory table row subline,
// the Properties Hub, the Register New Inventory Pool form's live
// preview, and the Quotation Catalog/My Order tables, so the format never
// drifts between them. Mirrors the backend's services/export_service.py
// format_money() but adds the thousands separator Intl.NumberFormat gives
// us for free.
//
// The active currency defaults to Naira but is overridable at runtime via
// setCurrencyCode() -- see components/quotation.js's loadPublicConfig(),
// which reads the real deployment value from GET /config/public
// (settings.CURRENCY_CODE, config.py) once on page load so this never has
// to be hardcoded here.
let _currencyCode = 'NGN';

export function setCurrencyCode(code) {
  if (code) _currencyCode = code;
}

// The <title> suffix ("— Login", "— My Assets", "— Manager", "— Super
// Admin") is baked into each page's own <title> tag and never changes at
// runtime -- captured once, the first time applySiteName() runs, so a
// second call (e.g. a re-fetch) never accidentally chains "Acme — Login
// — Login" onto itself.
let _titleSuffix = null;
let _titleSuffixCaptured = false;

// Applies settings.SITE_NAME (read once from GET /config/public --
// see components/quotation.js's loadPublicConfig(), called on EVERY page
// load including the unauthenticated login page) to the two places the
// deployment's name is shown on screen: the navbar/login brand (the
// `#siteBrandName` element present in index.html/admin.html/manager.html/
// staff.html/customer.html) and the browser tab's <title>. Keeps the
// two-tone "Word <muted>Word</muted>" styling the hardcoded "Snipe-IT
// Lite" brand used by splitting on the LAST space in the configured name
// (e.g. "Acme Corp" -> bold "Acme" + muted "Corp"); a single-word name
// just renders as one bold word, no muted half. A missing/empty
// site_name is a no-op -- whatever's already in the markup (the
// "Snipe-IT Lite" default) stays put rather than being blanked out.
export function applySiteName(siteName) {
  if (!siteName) return;
  const trimmed = String(siteName).trim();
  if (!trimmed) return;

  const brandEl = document.getElementById('siteBrandName');
  if (brandEl) {
    const lastSpace = trimmed.lastIndexOf(' ');
    if (lastSpace === -1) {
      brandEl.textContent = trimmed;
    } else {
      const lead = trimmed.slice(0, lastSpace);
      const tail = trimmed.slice(lastSpace + 1);
      brandEl.innerHTML = `${escapeHtml(lead)} <span class="font-medium text-slate-400">${escapeHtml(tail)}</span>`;
    }
  }

  if (!_titleSuffixCaptured) {
    const parts = document.title.split(' — ');
    _titleSuffix = parts.length > 1 ? parts.slice(1).join(' — ') : null;
    _titleSuffixCaptured = true;
  }
  document.title = _titleSuffix ? `${trimmed} — ${_titleSuffix}` : trimmed;
}

export function formatPrice(price) {
  if (price === null || price === undefined) return null;
  try {
    return new Intl.NumberFormat('en-NG', { style: 'currency', currency: _currencyCode }).format(price);
  } catch (e) {
    // Unknown/unsupported currency code -- fall back to a plain number
    // rather than letting Intl throw and break the whole render.
    return `${_currencyCode} ${Number(price).toFixed(2)}`;
  }
}

// =============================================================================
// SEARCH + PAGINATION (generic, reused by every listing table)
// -----------------------------------------------------------------------------
// The self-service "My Items" table (a single user's OWN checked-out
// items -- inherently small and bounded, since nobody has thousands of
// items in their own custody) is the one remaining table that works this
// way:
//   1. Fetch the FULL list from the API once and cache it in `tableState`.
//   2. Whenever the user types in the search box, changes "Rows per page",
//      or clicks Prev/Next, we DON'T hit the API again -- we just re-filter
//      and re-slice the cached array in memory and re-render, which keeps
//      the UI fast since filtering/pagination happen instantly client-side
//      instead of round-tripping to the server.
//   3. `renderMyItemsTable()` reads `tableState.myItems`, figures out which
//      "page" of rows to show, renders just those rows, and updates the
//      "Showing X-Y of Z" + Prev/Next button states.
//
// The Asset Inventory, User Directory, and Ad-Hoc Directory tables used to
// work this same way too (fetch one generously-sized page ONCE, then
// filter/paginate it in memory), but -- like the audit ledger before them
// (see components/audit.js's module docstring) -- those directories are
// unbounded in principle and can grow well past what's comfortable to hand
// the browser in one response. They now do TRUE server-side search +
// pagination instead: every keystroke (debounced), page turn, or "rows per
// page" change re-fetches just that slice from the API via `?search=&
// limit=&offset=`. Each of components/assets.js, components/users.js, and
// components/outsiders.js keeps its own small state object for this
// (mirroring `auditState` in components/audit.js) -- intentionally NOT
// entries in `tableState` below, since that machinery assumes the full
// dataset is already sitting in the browser, which is exactly what we're
// avoiding for these three now. `debounce()` and
// `renderServerPaginationBar()` further down in this file are the bits
// those three files (and audit.js) share.
// =============================================================================
export const tableState = {
  myItems: { raw: [], search: '', page: 1, perPage: 5 },
  // Self-service Equipment Quotation catalog (staff.html/customer.html) --
  // like My Items, this is a small, bounded list (every active asset pool)
  // so it uses the same fetch-once-then-filter-in-memory approach rather
  // than true server-side search/pagination. See components/quotation.js.
  // Default is 1 (not 5) at the user's request -- the catalog rows-per-page
  // selector on staff.html/customer.html only offers 1/3/5 (see the
  // #quotationCatalogPerPageSelect markup there) instead of the usual
  // 5/10/25/50 used elsewhere, so this needs to match its default option.
  quotationCatalog: { raw: [], search: '', page: 1, perPage: 1 },
};

// Maps a table key to the function that should re-render it. Each
// component registers its own render function via `registerRenderer()` once
// it's defined, so `setSearch`/`setPerPage`/`changePage` can call the right
// one without this module needing to import every component directly.
const RENDERERS = {};

export function registerRenderer(key, renderFn) {
  RENDERERS[key] = renderFn;
}

// Filters `rows` by `state.search` (case-insensitive substring match across
// `searchFields`), then slices out just the current page. Returns everything
// the caller needs to both render the rows and update the pagination bar.
export function filterAndPaginate(key, searchFields) {
  const state = tableState[key];
  let rows = state.raw;

  const query = state.search.trim().toLowerCase();
  if (query) {
    rows = rows.filter((row) =>
      searchFields.some((field) => String(row[field] ?? '').toLowerCase().includes(query))
    );
  }

  const total = rows.length;
  const totalPages = Math.max(1, Math.ceil(total / state.perPage));
  if (state.page > totalPages) state.page = totalPages;
  if (state.page < 1) state.page = 1;

  const startIndex = (state.page - 1) * state.perPage;
  const pageRows = rows.slice(startIndex, startIndex + state.perPage);

  return { pageRows, total, totalPages, startIndex };
}

// Updates the little "Showing 1-10 of 42" label and disables Prev/Next at
// the ends of the list. Safe to call even if a page doesn't have a
// pagination bar for this table (the elements just won't be found).
export function renderPaginationBar(key, total, startIndex, pageRowsLength) {
  const state = tableState[key];
  const infoEl = document.getElementById(`${key}PageInfo`);
  if (infoEl) {
    infoEl.textContent = total === 0
      ? 'No results found.'
      : `Showing ${startIndex + 1}-${startIndex + pageRowsLength} of ${total}`;
  }
  const totalPages = Math.max(1, Math.ceil(total / state.perPage));
  const prevBtn = document.getElementById(`${key}PrevBtn`);
  const nextBtn = document.getElementById(`${key}NextBtn`);
  if (prevBtn) prevBtn.disabled = state.page <= 1;
  if (nextBtn) nextBtn.disabled = state.page >= totalPages;
}

// Called from the search box's 'input' listener (wired in main.js).
export function setSearch(key, value) {
  if (!tableState[key]) return;
  tableState[key].search = value;
  tableState[key].page = 1; // always jump back to page 1 on a new search
  if (RENDERERS[key]) RENDERERS[key]();
}

// Called from the "Rows per page" <select>'s 'change' listener (main.js).
export function setPerPage(key, value) {
  if (!tableState[key]) return;
  tableState[key].perPage = parseInt(value, 10) || 5;
  tableState[key].page = 1;
  if (RENDERERS[key]) RENDERERS[key]();
}

// Called from the Prev (-1) / Next (+1) buttons' delegated click handler.
export function changePage(key, delta) {
  if (!tableState[key]) return;
  tableState[key].page += delta;
  if (RENDERERS[key]) RENDERERS[key]();
}

// =============================================================================
// SERVER-SIDE SEARCH + PAGINATION HELPERS
// -----------------------------------------------------------------------------
// Shared by every table that does TRUE server-side search/pagination
// (components/audit.js, components/assets.js, components/users.js,
// components/outsiders.js) -- as opposed to the client-side
// tableState/filterAndPaginate machinery above, which only "My Items"
// still uses.
// =============================================================================

// Delays calling `fn` until `delayMs` has passed without another call --
// used on the search `<input>`'s 'input' event so a true server-side table
// re-fetches once after someone finishes typing, rather than firing one API
// request per keystroke.
export function debounce(fn, delayMs = 300) {
  let timeoutId;
  return (...args) => {
    clearTimeout(timeoutId);
    timeoutId = setTimeout(() => fn(...args), delayMs);
  };
}

// Updates the "Showing X-Y of Z" label and enables/disables Prev/Next for a
// server-paginated table, given that table's own small `{ page, perPage,
// total }` state object (e.g. `auditState`, `assetsState`, `usersState`,
// `outsidersState`) and the `key` used to build its element ids
// (`${key}PageInfo` / `${key}PrevBtn` / `${key}NextBtn`). Safe to call even
// if a page doesn't have these elements.
export function renderServerPaginationBar(key, state) {
  const infoEl = document.getElementById(`${key}PageInfo`);
  const prevBtn = document.getElementById(`${key}PrevBtn`);
  const nextBtn = document.getElementById(`${key}NextBtn`);

  const startIndex = (state.page - 1) * state.perPage;
  const shownCount = Math.min(state.perPage, Math.max(0, state.total - startIndex));
  const totalPages = Math.max(1, Math.ceil(state.total / state.perPage));

  if (infoEl) {
    infoEl.textContent = state.total === 0
      ? 'No results found.'
      : `Showing ${startIndex + 1}-${startIndex + shownCount} of ${state.total}`;
  }
  if (prevBtn) prevBtn.disabled = state.page <= 1;
  if (nextBtn) nextBtn.disabled = state.page >= totalPages;
}

// =============================================================================
// PER-ITEM DISMISSAL PREFIX (shared with the "My Extension Decisions"
// notification feed below)
// -----------------------------------------------------------------------------
// This used to also back a signature-based "dismiss this whole banner until
// its content changes" mechanism for the Overdue/Due Soon/Extension
// Requests banners, back when they were always-visible dashboard banners.
// Now that they render inside the Notification Center bell's dropdown
// instead (closed by default, opened on demand -- see
// js/components/notifications.js), that mechanism is no longer needed:
// there's nothing to "dismiss", the dropdown just always reflects whatever
// is currently true whenever it's opened. The prefix itself lives on here
// because the per-item dismissal helpers just below (isItemDismissed()/
// dismissItems(), still used by the "My Extension Decisions" feed) share
// the same localStorage namespace.
const DISMISS_STORAGE_PREFIX = 'snipeit:alertDismissed:';

// =============================================================================
// PER-ITEM DISMISSAL (My Extension Decisions banner)
// -----------------------------------------------------------------------------
// The signature-based helpers above are the right model for Overdue/Due
// Soon/Extension Requests: those banners always describe the CURRENT
// state of the world ("these items are overdue right now"), so it's
// correct for the whole banner to reappear the moment that state changes
// even slightly -- there's always exactly one aggregate thing to dismiss.
//
// "My Extension Decisions" is a different shape: a feed of discrete,
// one-time notification events ("your request was approved"), much closer
// to a notification inbox than a live status banner. If dismissal were
// keyed to a signature of the WHOLE list (like the helpers above), then
// approving/denying one MORE request later would change that signature and
// bring back every OTHER decision the person already dismissed, bundled in
// with the one new one -- "I already saw and closed this, why is it back?"
// Instead, each decision's own id is added to a small persisted SET the
// first time it's dismissed, and is filtered out of the list on every
// future load forever after, however many newer decisions arrive alongside
// it. A decision only ever needs to be dismissed once, and dismissing it
// never affects any other decision's visibility.
//
// Capped at MAX_DISMISSED_IDS entries (oldest trimmed first) purely as a
// defensive ceiling on localStorage growth -- in practice this never gets
// close, since the server itself only ever returns decisions from the last
// DECISION_ALERT_WINDOW_DAYS (see extension_service.py), so anything older
// naturally stops being offered for dismissal at all.
const MAX_DISMISSED_IDS = 200;

function _readDismissedIdSet(key) {
  try {
    const raw = window.localStorage.getItem(DISMISS_STORAGE_PREFIX + key + ':ids');
    return raw ? new Set(JSON.parse(raw)) : new Set();
  } catch (e) {
    return new Set(); // corrupt/missing/private-mode -- fail open, same as isAlertDismissed()
  }
}

export function isItemDismissed(key, id) {
  return _readDismissedIdSet(key).has(id);
}

// Marks every id in `ids` as dismissed (adds to the existing set rather
// than replacing it -- previously-dismissed ids from earlier visits are
// preserved). Safe to call with an empty array.
export function dismissItems(key, ids) {
  if (!ids || !ids.length) return;
  try {
    const merged = _readDismissedIdSet(key);
    ids.forEach(id => merged.add(id));
    // Oldest-first trim: Set preserves insertion order in JS, so slicing
    // from the front of the array drops the oldest entries once over cap.
    const trimmed = Array.from(merged).slice(-MAX_DISMISSED_IDS);
    window.localStorage.setItem(DISMISS_STORAGE_PREFIX + key + ':ids', JSON.stringify(trimmed));
  } catch (e) {
    // Ignore -- worst case a dismissal only lasts for this page load.
  }
}


// A person-level (not item-level) alert icon for the User Directory /
// Ad-Hoc Directory tables. Takes the `alerts: {overdue, due_soon,
// pending_extension}` object backend/services/user_service.py's
// list_users() (and outsider_service.py's list_outsiders()) computes per
// person -- ONE icon regardless of how many individual items are overdue/
// due soon/pending, exactly the "don't flag someone once per item" ask
// this was built for. Colored by the single most urgent thing true for
// them (overdue > due soon > pending extension); the tooltip names
// everything that applies, and the Custody Ledger (opened via the
// existing "Custody Ledger" button on that row) is always where the
// actual itemized detail lives -- this icon is only ever a pointer to it.
export function personAlertIcon(alerts) {
  if (!alerts || (!alerts.overdue && !alerts.due_soon && !alerts.pending_extension)) return '';
  const reasons = [];
  if (alerts.overdue) reasons.push('has overdue item(s)');
  if (alerts.due_soon) reasons.push('has item(s) due soon');
  if (alerts.pending_extension) reasons.push('has a pending extension request');
  const color = alerts.overdue ? 'text-rose-400' : alerts.due_soon ? 'text-amber-400' : 'text-violet-400';
  const title = `${reasons.join(' · ')} — see Custody Ledger for details`;
  return `<span title="${escapeHtml(title)}" class="inline-flex h-4 w-4 shrink-0 ${color}">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M12 9v4m0 4h.01M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0Z"/></svg>
  </span>`;
}

// Groups a list of per-checkout alert items (each already carrying
// assignee_name/entity_id/entity_type -- see services/checkout_service.py's
// list_overdue_checkouts()/list_due_soon_checkouts() and
// services/extension_service.py's list_extension_requests()) into one
// entry per PERSON. This is what lets the Overdue/Due Soon banners and the
// Extension Requests panel show "T. Okafor has 2 items due soon" instead
// of two separate item rows -- someone with a pile of checked-out
// equipment never gets flagged once per item, just once per alert type.
export function groupByPerson(items) {
  const groups = new Map();
  for (const item of items) {
    const key = item.entity_type && item.entity_id != null ? `${item.entity_type}:${item.entity_id}` : `name:${item.assignee_name}`;
    if (!groups.has(key)) {
      groups.set(key, { entityId: item.entity_id, entityType: item.entity_type, assigneeName: item.assignee_name, items: [] });
    }
    groups.get(key).items.push(item);
  }
  return Array.from(groups.values());
}

export function statusBadge(available) {
  if (available <= 3) {
    return `<span class="inline-flex items-center gap-1 rounded-full bg-amber-500/10 px-2.5 py-1 text-[11px] font-semibold text-amber-400 ring-1 ring-amber-500/30"><span class="h-1.5 w-1.5 rounded-full bg-amber-500"></span> Critical Low Stock</span>`;
  }
  return `<span class="inline-flex items-center gap-1 rounded-full bg-emerald-500/10 px-2.5 py-1 text-[11px] font-semibold text-emerald-400 ring-1 ring-emerald-500/30"><span class="h-1.5 w-1.5 rounded-full bg-emerald-500"></span> In Stock</span>`;
}

// -----------------------------------------------------------------------------
// Search box clear ("x") buttons -- every search input across all four
// dashboards (Asset Inventory, User Directory, Deleted Users, Outsiders,
// Quotes tab, Quote Detail's "Add item"/"Assign to user" boxes, My Items,
// Quotation Catalog) shares the exact same markup shape: a text <input>
// carrying the `search-clearable` marker class, sitting as the sole/first
// child of a `position: relative` wrapper `<div>` (already required for
// each one's search icon, positioned via `absolute left-3`). That
// consistent shape means one generic pass at startup can inject a
// matching `absolute right-*` clear button for every one of them, rather
// than hand-duplicating a button element in 19 places across admin.html/
// manager.html/staff.html/customer.html. Called once from main.js's
// DOMContentLoaded handler, same as wireTableControls() -- every one of
// these inputs already exists in the static HTML at that point (some
// just sit inside an initially-hidden modal), so no re-init is needed
// later when a modal opens or a table re-renders.
export function initSearchClearButtons() {
  document.querySelectorAll('input.search-clearable').forEach((input) => {
    const wrapper = input.parentElement;
    // Guard against double-injection (e.g. if this were ever called
    // twice) -- look for a button this function already added.
    if (!wrapper || wrapper.querySelector('.search-clear-btn')) return;

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'search-clear-btn hidden absolute right-2.5 top-1/2 -translate-y-1/2 rounded p-0.5 text-slate-500 transition hover:text-slate-200';
    btn.setAttribute('aria-label', 'Clear search');
    btn.innerHTML = '<svg class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" d="M18 6L6 18M6 6l12 12"/></svg>';
    wrapper.appendChild(btn);

    const toggle = () => btn.classList.toggle('hidden', input.value.length === 0);
    toggle();
    input.addEventListener('input', toggle);

    btn.addEventListener('click', () => {
      input.value = '';
      // Fire a real 'input' event rather than calling each page's own
      // setSearch()/search function directly -- every search box here is
      // already wired to one via addEventListener('input', ...) in
      // main.js's wireTableControls(), so dispatching this re-uses that
      // exact same listener (debounced server search, client-side
      // filterAndPaginate, or a typeahead) with zero extra wiring here.
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.focus();
      toggle();
    });
  });
}
