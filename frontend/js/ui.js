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

export function escapeHtml(str) {
  if (str === null || str === undefined) return '';
  return String(str).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// -----------------------------------------------------------------------------
// MOBILE "ROW DETAILS" POPUP
// -----------------------------------------------------------------------------
// Every data table in this app hides its less-critical columns below the
// `sm` (640px) breakpoint -- see each components/*.js render function's
// `hidden sm:table-cell` cells -- so a table never forces the page into a
// long horizontal scroll on a phone. Desktop (`sm:` and up) still renders
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
  const logs = document.getElementById('auditBackupsSection');
  const tabAssets = document.getElementById('tabAssets');
  const tabUsers = document.getElementById('tabUsers');
  const tabAdhoc = document.getElementById('tabAdhoc');
  const tabLogs = document.getElementById('tabLogs');
  if (!assets || !users) return;

  const activeCls = ['border-blue-500', 'text-slate-50', 'font-semibold'];
  const inactiveCls = ['border-transparent', 'text-slate-500', 'font-medium'];
  const allTabs = [tabAssets, tabUsers, tabAdhoc, tabLogs].filter(Boolean);
  const allSections = [assets, users, adhoc, logs].filter(Boolean);

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
// switchTab() above) -- admin.html has 4 tabs (Asset Inventory / User
// Directory / Ad-Hoc Directory / Audit & Backups), manager.html only 3
// (no Audit & Backups tab there -- see admin.html's own comment on
// #auditBackupsSection for why backups are Super Admin/Admin-only).
// getSwipeTabOrder()'s `document.getElementById` filter below means both
// pages "just work" here without any per-page branching; on every other
// page the `#tabAssets` lookup fails fast and both functions are no-ops.
//
// The order swiping moves through always matches left-to-right reading
// order of the tabs actually present on THIS page/role (Ad-Hoc doesn't
// exist for every role), rather than a hardcoded list, so it can't ever
// try to switch to a tab that isn't there.
function getSwipeTabOrder() {
  return ['assets', 'users', 'adhoc', 'logs'].filter(t => document.getElementById(`tab${t[0].toUpperCase()}${t.slice(1)}`));
}

function getActiveSwipeTab(order) {
  const sectionIds = { assets: 'assetInventorySection', users: 'userDirectorySection', adhoc: 'adhocDirectorySection', logs: 'auditBackupsSection' };
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

export function toggleRoute() {
  const val = document.getElementById('routeSelect').value;
  document.getElementById('staffField').classList.toggle('hidden', val !== 'staff');
  document.getElementById('customerField').classList.toggle('hidden', val !== 'customer');
  document.getElementById('adhocField').classList.toggle('hidden', val !== 'adhoc');
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
}

// Same show/hide toggle as toggleNameEdit() above, for the Properties
// Hub's department field (admin.html only -- see components/assets.js's
// saveDepartment()/openPropsModal()).
export function toggleDepartmentEdit() {
  document.getElementById('deptDisplay').classList.toggle('hidden');
  const edit = document.getElementById('deptEdit');
  edit.classList.toggle('hidden');
  edit.classList.toggle('flex');
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
// DISMISSIBLE ALERT BANNERS (Overdue / Due Soon / Extension Requests)
// -----------------------------------------------------------------------------
// Each of those three banners has the exact same "click X, it goes away,
// but a routine background refresh shouldn't immediately pop it back open"
// requirement. A plain in-memory flag handles that within a single page
// load, but it resets the instant the tab is reloaded or the person
// navigates away and back -- from a user's point of view that looks
// exactly like "clicking X did nothing", since the banner they *just*
// dismissed is right back the next time they land on the page.
//
// This keeps the dismissal in localStorage instead, keyed to a signature
// of exactly what was on screen when it was dismissed (e.g. which
// checkout ids, how many days until due). That means:
//   - Reloading the page / switching tabs and back respects the dismissal.
//   - The moment the underlying situation actually changes -- a new item
//     becomes due soon, one drops off, a days-until-due count ticks over --
//     the signature no longer matches, so the banner comes back on its own
//     even without an explicit "Check Alerts" click, since that's now
//     genuinely new information the person hasn't seen and dismissed yet.
const DISMISS_STORAGE_PREFIX = 'snipeit:alertDismissed:';

export function isAlertDismissed(key, signature) {
  if (!signature) return false;
  try {
    return window.localStorage.getItem(DISMISS_STORAGE_PREFIX + key) === signature;
  } catch (e) {
    // Storage can throw in some private-browsing modes -- fail open (i.e.
    // just show the banner) rather than let a storage quirk crash the page.
    return false;
  }
}

export function setAlertDismissed(key, signature) {
  if (!signature) return;
  try {
    window.localStorage.setItem(DISMISS_STORAGE_PREFIX + key, signature);
  } catch (e) {
    // Ignore -- worst case the dismissal only lasts for this page load.
  }
}

export function clearAlertDismissed(key) {
  try {
    window.localStorage.removeItem(DISMISS_STORAGE_PREFIX + key);
  } catch (e) {
    // Ignore, same as above.
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
