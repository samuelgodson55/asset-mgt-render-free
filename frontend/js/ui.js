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

export function escapeHtml(str) {
  if (str === null || str === undefined) return '';
  return String(str).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
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
  return d.toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric',
    hour: 'numeric', minute: '2-digit',
  });
}

export function switchTab(tab) {
  const assets = document.getElementById('assetInventorySection');
  const users = document.getElementById('userDirectorySection');
  const adhoc = document.getElementById('adhocDirectorySection');
  const tabAssets = document.getElementById('tabAssets');
  const tabUsers = document.getElementById('tabUsers');
  const tabAdhoc = document.getElementById('tabAdhoc');
  if (!assets || !users) return;

  const activeCls = ['border-blue-500', 'text-slate-50', 'font-semibold'];
  const inactiveCls = ['border-transparent', 'text-slate-500', 'font-medium'];
  const allTabs = [tabAssets, tabUsers, tabAdhoc].filter(Boolean);
  const allSections = [assets, users, adhoc].filter(Boolean);

  allSections.forEach(s => s.classList.add('hidden'));
  allTabs.forEach(t => { t.classList.add(...inactiveCls); t.classList.remove(...activeCls); });

  if (tab === 'assets') {
    assets.classList.remove('hidden');
    tabAssets.classList.add(...activeCls); tabAssets.classList.remove(...inactiveCls);
  } else if (tab === 'adhoc' && adhoc) {
    adhoc.classList.remove('hidden');
    tabAdhoc.classList.add(...activeCls); tabAdhoc.classList.remove(...inactiveCls);
  } else {
    users.classList.remove('hidden');
    tabUsers.classList.add(...activeCls); tabUsers.classList.remove(...inactiveCls);
  }
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
  myItems: { raw: [], search: '', page: 1, perPage: 10 },
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
  tableState[key].perPage = parseInt(value, 10) || 10;
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

export function statusBadge(available) {
  if (available <= 3) {
    return `<span class="inline-flex items-center gap-1 rounded-full bg-amber-500/10 px-2.5 py-1 text-[11px] font-semibold text-amber-400 ring-1 ring-amber-500/30"><span class="h-1.5 w-1.5 rounded-full bg-amber-500"></span> Critical Low Stock</span>`;
  }
  return `<span class="inline-flex items-center gap-1 rounded-full bg-emerald-500/10 px-2.5 py-1 text-[11px] font-semibold text-emerald-400 ring-1 ring-emerald-500/30"><span class="h-1.5 w-1.5 rounded-full bg-emerald-500"></span> In Stock</span>`;
}
