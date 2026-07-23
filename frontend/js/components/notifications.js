// =============================================================================
// js/components/notifications.js
// -----------------------------------------------------------------------------
// THE NOTIFICATION CENTER -- a single bell icon (in every dashboard's
// navbar) with an unread-style badge count, replacing what used to be a
// header "Check Alerts" refresh button plus a stack of always-visible
// dashboard banners (Overdue / Due Soon / Extension Requests / My Extension
// Decisions / "All caught up").
//
// WHY A BELL INSTEAD OF BANNERS
// -----------------------------------------------------------------------------
// The old banners were always on-screen the moment there was anything to
// report, pushing the actual tables further down the page, and each one
// needed its own bespoke "dismiss, but don't immediately pop back open"
// logic (see the old js/ui.js isAlertDismissed()/setAlertDismissed()).
// Moving everything into a closed-by-default dropdown means:
//   - The dashboard itself stays clean; nothing appears unless the person
//     actually opens the bell.
//   - No dismiss/recall bookkeeping is needed for "live state" categories
//     (Overdue/Due Soon/Extension Requests/my own item alerts) -- the
//     dropdown just always shows whatever is currently true when opened.
//   - Clicking a notification IS the action -- an Overdue/Due Soon or
//     pending Extension Request entry opens straight into that person's
//     Custody Ledger (where Approve/Deny for a pending request actually
//     happens -- see components/custody.js), and a personal Due Soon/
//     Overdue notification opens the Request Extension modal directly.
//
// WHO SEES WHAT (personalized per role)
// -----------------------------------------------------------------------------
//   - Super Admin / Manager (admin.html, manager.html): the review-facing
//     sections -- Overdue Checkouts, Due Soon, and pending Extension
//     Requests awaiting THEIR decision (see components/overdue.js,
//     components/due-soon.js, components/extensions.js) -- PLUS the
//     personal sections below, since a Super Admin/Manager can also have
//     their own checked-out items.
//   - Staff / Customer (staff.html, customer.html): only the personal
//     sections -- their own items overdue/due soon, their own pending
//     extension requests, and updates on decisions made about THEIR
//     requests. They never see the review-facing sections (the backend
//     endpoints those call are privileged-role-only anyway -- see
//     backend/api/checkouts.py's require_privileged_role dependency).
//
// Every load function below is a no-op (returns false immediately) on any
// page that doesn't have the corresponding section markup in its bell
// dropdown, exactly the same "no-op when the element doesn't exist" pattern
// already used throughout this codebase (e.g. loadDeletedUsers() in
// components/users.js) -- so this one module works unmodified across all
// four dashboard pages.
// =============================================================================

import { apiRequest } from '../api.js';
import { escapeHtml } from '../ui.js';
import { loadOverdueAlerts } from './overdue.js';
import { loadDueSoonAlerts } from './due-soon.js';
import { loadExtensionRequests, loadMyExtensionDecisionsAlert } from './extensions.js';

// Sections that only ever need to toggle their own `hidden` class based on
// a count -- each entry here is [section element id, count element id].
// Used both to compute the bell's total badge count and to know whether
// literally nothing has anything to show (-> empty state).
const NOTIFICATION_SECTIONS = [
  ['overdueAlertBanner', 'overdueAlertCount'],
  ['dueSoonAlertBanner', 'dueSoonAlertCount'],
  ['extensionRequestsPanel', 'extensionRequestsCount'],
  ['myExtensionDecisionsBanner', 'myExtensionDecisionsCount'],
  ['notifMyOverdueSection', 'notifMyOverdueCount'],
  ['notifMyDueSoonSection', 'notifMyDueSoonCount'],
  ['notifMyPendingExtensionSection', 'notifMyPendingExtensionCount'],
];

function updateBadge() {
  const badge = document.getElementById('notificationBadge');
  const emptyLabel = document.getElementById('notificationEmptyLabel');
  const emptyState = document.getElementById('notificationEmptyState');
  if (!badge) return; // this page has no bell (shouldn't happen, but fail quietly)

  let total = 0;
  let anySectionVisible = false;
  for (const [sectionId, countId] of NOTIFICATION_SECTIONS) {
    const section = document.getElementById(sectionId);
    const countEl = document.getElementById(countId);
    if (section && countEl && !section.classList.contains('hidden')) {
      anySectionVisible = true;
      total += parseInt(countEl.textContent, 10) || 0;
    }
  }

  if (total > 0) {
    badge.textContent = total > 99 ? '99+' : String(total);
    badge.classList.remove('hidden');
  } else {
    badge.classList.add('hidden');
  }

  if (emptyLabel) emptyLabel.classList.toggle('hidden', anySectionVisible);
  if (emptyState) emptyState.classList.toggle('hidden', anySectionVisible);
}

// ---- Personal item alerts (all four dashboards) ---------------------------
// Everyone -- Super Admin, Manager, Staff, Customer alike -- can have their
// own checked-out items (see backend/services/user_service.py's
// get_my_assigned_items()), so this runs on every page regardless of role.
// Reuses the exact same overdue/due_soon/pending_extension flags already
// computed server-side for js/components/myitems.js's "My Checked-Out
// Items" table -- just regrouped here into bell notifications instead of
// per-row badges.
//
// BUG FIX: each of the three sections below now has its own header (icon +
// count + label) in the HTML markup, matching overdueAlertBanner/
// dueSoonAlertBanner/extensionRequestsPanel. Previously the header markup
// (and therefore the "...Count" element renderPersonalSection() writes to)
// didn't exist at all on any dashboard page -- so the instant a Staff/
// Customer account actually had an overdue item, a due-soon item, or a
// pending extension request, `document.getElementById(countId).textContent
// = ...` threw on a null element, the surrounding try/catch in
// loadPersonalItemAlerts() caught it and hid ALL THREE personal sections
// (not just the one that failed), and the bell silently showed "all caught
// up" instead. That's why Staff/Customer accounts never saw feedback about
// their own requests. renderPersonalSection() below now also guards with
// `if (countEl)` so a future markup omission degrades gracefully instead of
// blanking every personal section again.
export async function loadPersonalItemAlerts() {
  const overdueSection = document.getElementById('notifMyOverdueSection');
  const dueSoonSection = document.getElementById('notifMyDueSoonSection');
  const pendingSection = document.getElementById('notifMyPendingExtensionSection');
  if (!overdueSection && !dueSoonSection && !pendingSection) return false;

  try {
    const data = await apiRequest('/users/me/items');
    const items = data.assigned_items || [];

    const overdueItems = items.filter(i => i.overdue);
    const dueSoonItems = items.filter(i => i.due_soon && !i.overdue);
    const pendingItems = items.filter(i => i.pending_extension);

    renderPersonalSection(overdueSection, 'notifMyOverdueCount', 'notifMyOverdueList', overdueItems, {
      rowClass: 'hover:text-rose-300',
      countColor: 'text-rose-400',
      actionLabel: 'Request extension →',
    });
    renderPersonalSection(dueSoonSection, 'notifMyDueSoonCount', 'notifMyDueSoonList', dueSoonItems, {
      rowClass: 'hover:text-amber-200',
      countColor: 'text-amber-400',
      actionLabel: 'Request extension →',
    });
    renderPersonalSection(pendingSection, 'notifMyPendingExtensionCount', 'notifMyPendingExtensionList', pendingItems, {
      rowClass: '',
      countColor: 'text-violet-400',
      actionLabel: null, // informational only -- nothing to click through to yet
    });

    return true;
  } catch (err) {
    // Fail quietly -- same "never block the rest of the bell" rule as
    // every other notification loader.
    [overdueSection, dueSoonSection, pendingSection].forEach(s => s && s.classList.add('hidden'));
    console.error('Failed to load personal item alerts:', err.message);
    return false;
  }
}

function renderPersonalSection(section, countId, listId, items, opts) {
  if (!section) return;
  if (!items.length) {
    section.classList.add('hidden');
    return;
  }
  const countEl = document.getElementById(countId);
  if (countEl) countEl.textContent = items.length;
  const list = document.getElementById(listId);
  list.innerHTML = items.map(item => `
  <li class="flex items-center justify-between gap-3 py-1 ${opts.actionLabel ? `cursor-pointer transition ${opts.rowClass}` : ''}"
    ${opts.actionLabel ? `data-action="open-extension-request" data-checkout-id="${item.checkout_id}" data-asset-name="${escapeHtml(item.asset_name)}" data-due-date="${escapeHtml(item.due_date)}" data-action-notification="1"` : ''}>
    <span class="truncate text-slate-300">${escapeHtml(item.asset_name)} <span class="text-slate-500">· due ${escapeHtml(item.due_date)}</span></span>
    ${opts.actionLabel ? `<span class="shrink-0 tag-mono text-[11px] font-semibold ${opts.countColor}">${opts.actionLabel}</span>` : ''}
  </li>`).join('');
  section.classList.remove('hidden');
}

// ---- Coordinator: reloads every section this page has, then the badge ----
export async function refreshNotifications() {
  await Promise.all([
    loadOverdueAlerts(),
    loadDueSoonAlerts(),
    loadExtensionRequests(),
    loadMyExtensionDecisionsAlert(),
    loadPersonalItemAlerts(),
  ]);
  updateBadge();
}

// ---- Bell open/close ------------------------------------------------------

let isOpen = false;

// ---- Responsive positioning ------------------------------------------------
// The dropdown used to rely purely on Tailwind's `absolute right-0` inside
// a `relative` wrapper around JUST the bell button. That wrapper is only
// as wide as the button itself -- it is NOT the rightmost thing in the
// navbar (the avatar, theme toggle, and Log out button all sit to its
// right, see admin.html/manager.html/staff.html/customer.html's navbar
// markup) -- so `right-0` anchored the dropdown's right edge to the
// bell button's own edge, not to the actual right edge of the viewport.
// On anything short of a very wide window, a 22-24rem-wide dropdown
// anchored that far from the true edge ran off the LEFT side of the
// viewport instead -- invisibly, since `<body>` has `overflow-x-hidden`,
// so the cut-off text in the screenshots wasn't a scrollbar situation,
// it was actually rendered off-screen.
//
// Fixed positioning computed from the button's own live bounding rect
// (viewport coordinates, always correct regardless of ancestor widths,
// other navbar icons, zoom level, or screen size) replaces that reliance
// on ancestor width entirely, and is clamped so the panel can never
// extend past either edge.
//
// The panel is horizontally CENTERED in the viewport rather than anchored
// to the bell's own left/right edge -- the bell itself sits left-of-center
// in every navbar (the Staff Portal/Admin Mode pill, avatar, theme toggle,
// and Log out button all sit further right of it), so anchoring to the
// bell made the dropdown look pushed off to one side instead of sitting
// in a natural, predictable spot. Only the vertical position (`top`) is
// still derived from the bell's own rect, so the panel always opens
// directly below the navbar no matter the page.
const VIEWPORT_MARGIN = 8;

function positionNotificationDropdown() {
  const dropdown = document.getElementById('notificationDropdown');
  const btn = document.getElementById('notificationBellBtn');
  if (!dropdown || !btn) return;

  const rect = btn.getBoundingClientRect();
  const width = Math.min(384, window.innerWidth - VIEWPORT_MARGIN * 2); // 384px = 24rem
  let left = (window.innerWidth - width) / 2; // centered in the viewport
  left = Math.max(VIEWPORT_MARGIN, Math.min(left, window.innerWidth - width - VIEWPORT_MARGIN));

  dropdown.style.position = 'fixed';
  dropdown.style.left = `${left}px`;
  dropdown.style.right = 'auto';
  dropdown.style.top = `${rect.bottom + 8}px`;
  dropdown.style.width = `${width}px`;
  dropdown.style.maxWidth = 'none'; // the inline width above already accounts for the viewport
}

export function toggleNotificationDropdown() {
  const dropdown = document.getElementById('notificationDropdown');
  const btn = document.getElementById('notificationBellBtn');
  if (!dropdown) return;
  isOpen = dropdown.classList.contains('hidden');
  if (isOpen) positionNotificationDropdown();
  dropdown.classList.toggle('hidden', !isOpen);
  if (btn) btn.setAttribute('aria-expanded', String(isOpen));
  // Opening the bell is exactly when a person wants the freshest picture --
  // refresh right then, on top of whatever background refreshes already
  // happened, so it never shows stale-by-a-few-minutes data.
  if (isOpen) refreshNotifications();
}

export function closeNotificationDropdown() {
  const dropdown = document.getElementById('notificationDropdown');
  const btn = document.getElementById('notificationBellBtn');
  if (!dropdown) return;
  isOpen = false;
  dropdown.classList.add('hidden');
  if (btn) btn.setAttribute('aria-expanded', 'false');
}

// Click-outside-to-close + click-a-notification-to-close: any element
// carrying `data-action-notification="1"` (set on every clickable
// notification row across overdue.js/due-soon.js/extensions.js/this file)
// closes the dropdown right after its own click handler runs, so acting on
// a notification always feels like it "took you somewhere" rather than
// leaving a stale dropdown hanging open over whatever modal/drawer just
// opened underneath it.
export function initNotificationBell() {
  const dropdown = document.getElementById('notificationDropdown');
  if (!dropdown) return; // page has no bell (shouldn't happen)

  document.addEventListener('click', (event) => {
    const withinBell = event.target.closest('#notificationBellBtn, #notificationDropdown');
    const actionEl = event.target.closest('[data-action-notification]');
    if (actionEl) {
      // Let the actual data-action handler (already wired via event
      // delegation in main.js) run first, then close on the next tick.
      setTimeout(closeNotificationDropdown, 0);
      return;
    }
    if (!withinBell && isOpen) closeNotificationDropdown();
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && isOpen) closeNotificationDropdown();
  });

  // A resize (rotating a phone, resizing a desktop window) while the
  // dropdown is open should re-clamp it to the new viewport instead of
  // leaving it positioned for the old one.
  window.addEventListener('resize', () => {
    if (isOpen) positionNotificationDropdown();
  });
}
