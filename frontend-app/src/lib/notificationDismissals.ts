// lib/notificationDismissals.ts
// -----------------------------------------------------------------------------
// Tracks which alerts the signed-in browser has already dismissed, so they
// don't keep reappearing forever -- there's no server-side "delete" for
// either feed (quotation notifications only ever get `read_at`, decisions
// have no per-row state at all), so "cleared" is purely a client-side,
// per-browser concept persisted to localStorage. Shared by
// components/NotificationBell.tsx (needs the dismissed sets to keep its
// badge count accurate) and pages/Notifications.tsx (renders the dismiss
// buttons and writes to these same stores) -- previously duplicated inline
// inside the bell dropdown before that content moved onto its own page.
//
// Two independent id spaces/keys, since QuotationNotification.id and
// MyExtensionDecision.id are unrelated primary keys that can and do
// collide in value -- sharing one set would cross-dismiss the wrong rows.
// -----------------------------------------------------------------------------

const MAX_DISMISSED_IDS = 300;
const DECISIONS_DISMISSED_KEY = "ledger:myExtensionDecisionsDismissed";
const QUOTATION_NOTIFICATIONS_DISMISSED_KEY = "ledger:myQuotationNotificationsDismissed";

function readIdSet(key: string): Set<number> {
  try {
    const raw = window.localStorage.getItem(key);
    return new Set(raw ? (JSON.parse(raw) as number[]) : []);
  } catch {
    return new Set();
  }
}

function dismissIds(key: string, ids: number[]) {
  if (!ids.length) return;
  try {
    const merged = readIdSet(key);
    ids.forEach((id) => merged.add(id));
    const trimmed = Array.from(merged).slice(-MAX_DISMISSED_IDS);
    window.localStorage.setItem(key, JSON.stringify(trimmed));
  } catch {
    // Worst case a dismissal only lasts this page load -- same "ignore
    // storage errors" rule the legacy frontend's dismissItems() used.
  }
}

export function readDismissedSet(): Set<number> {
  return readIdSet(DECISIONS_DISMISSED_KEY);
}

export function dismissDecisionIds(ids: number[]) {
  dismissIds(DECISIONS_DISMISSED_KEY, ids);
}

export function readDismissedQuotationNotificationSet(): Set<number> {
  return readIdSet(QUOTATION_NOTIFICATIONS_DISMISSED_KEY);
}

export function dismissQuotationNotificationIds(ids: number[]) {
  dismissIds(QUOTATION_NOTIFICATIONS_DISMISSED_KEY, ids);
}
