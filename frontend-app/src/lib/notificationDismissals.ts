// lib/notificationDismissals.ts
// -----------------------------------------------------------------------------
// Tracks which "Extension decision" alerts (approved/denied, from GET
// /checkouts/my-extension-decisions) the signed-in browser has already
// dismissed, so they don't keep reappearing forever. Shared by
// components/NotificationBell.tsx (needs the dismissed set to keep its badge
// count accurate) and pages/Notifications.tsx (renders the dismiss button and
// writes to this same store) -- previously duplicated inline inside the bell
// dropdown before that content moved onto its own page.
// -----------------------------------------------------------------------------

const MAX_DISMISSED_IDS = 300;
const DECISIONS_DISMISSED_KEY = "ledger:myExtensionDecisionsDismissed";

export function readDismissedSet(): Set<number> {
  try {
    const raw = window.localStorage.getItem(DECISIONS_DISMISSED_KEY);
    return new Set(raw ? (JSON.parse(raw) as number[]) : []);
  } catch {
    return new Set();
  }
}

export function dismissDecisionIds(ids: number[]) {
  if (!ids.length) return;
  try {
    const merged = readDismissedSet();
    ids.forEach((id) => merged.add(id));
    const trimmed = Array.from(merged).slice(-MAX_DISMISSED_IDS);
    window.localStorage.setItem(DECISIONS_DISMISSED_KEY, JSON.stringify(trimmed));
  } catch {
    // Worst case a dismissal only lasts this page load -- same "ignore
    // storage errors" rule the legacy frontend's dismissItems() used.
  }
}
