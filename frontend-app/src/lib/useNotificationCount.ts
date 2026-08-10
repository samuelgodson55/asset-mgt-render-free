import { useEffect, useState } from "react";
import { alertsApi, extensionsApi, myItemsApi, quotationsApi } from "./api";
import { readDismissedSet, readDismissedQuotationNotificationSet } from "./notificationDismissals";

// =============================================================================
// lib/useNotificationCount.ts
// -----------------------------------------------------------------------------
// Single source of truth for "how many things need my attention right now",
// shared by components/NotificationBell.tsx (the header badge) and
// components/Layout.tsx (the sidebar "Notifications" nav badge). Both used
// to compute this total independently -- the bell had its own inline
// Promise.all, the sidebar derived its count from api.getNotifications()'s
// flattened NotificationItem[] (which used a totally different, and
// incomplete, formula: it never included quotation notifications or
// pending/my-extension-decisions at all, and its `read` flag was hardcoded
// per notification kind rather than reflecting real read/dismissed state).
// That drift is exactly why the two badges could show different numbers
// side by side on the same page. pages/Notifications.tsx (the full page
// both badges link to) already had the correct, complete formula -- this
// hook lifts that same formula out so every place showing a count agrees
// with what a person would actually count on that page.
//
// REGRESSION THIS ALSO FIXES: the bell's old inline version fetched
// quotationsApi.myNotifications() without a .catch(), unlike every other
// call in the same Promise.all. tryLoad() (see lib/api.ts) only swallows a
// failed request in demo mode -- on a real, logged-in session any hiccup
// fetching that one endpoint (network blip, a transient 5xx, etc.) rejected
// the whole Promise.all, so the surrounding async function threw, `setCount`
// was never called, and the badge silently stayed hidden at 0 forever after
// that, even once everything else on the page loaded fine. Every request
// below is now caught with its own fallback so one flaky request can never
// blank out the whole count.
// =============================================================================

export function useNotificationCount(privileged: boolean): number {
  const [count, setCount] = useState(0);

  useEffect(() => {
    let cancelled = false;
    const dismissedDecisions = readDismissedSet();
    const dismissedQuotationNotifications = readDismissedQuotationNotificationSet();

    const run = async () => {
      const [myItemsRes, decisions, overdue, dueSoon, extensions, quotationNotifications] = await Promise.all([
        myItemsApi.list().catch(() => ({ assigned_items: [] })),
        extensionsApi.myDecisions(10).catch(() => []),
        privileged ? alertsApi.overdue(5) : Promise.resolve({ items: [], total: 0 }),
        privileged ? alertsApi.dueSoon(5) : Promise.resolve({ items: [], total: 0 }),
        privileged ? extensionsApi.listPending().catch(() => []) : Promise.resolve([]),
        quotationsApi.myNotifications().catch(() => []),
      ]);
      if (cancelled) return;

      const items = myItemsRes.assigned_items ?? [];
      const myOverdue = items.filter((i) => i.overdue).length;
      const myDueSoon = items.filter((i) => i.due_soon && !i.overdue).length;
      const myPending = items.filter((i) => i.pending_extension).length;
      const undismissedDecisions = decisions.filter((d) => !dismissedDecisions.has(d.id)).length;
      const pendingExtensions = extensions.filter((e) => e.status === "pending").length;
      const unreadQuotationNotifications = quotationNotifications.filter(
        (n) => !n.read_at && !dismissedQuotationNotifications.has(n.id),
      ).length;

      setCount(
        overdue.total + dueSoon.total + pendingExtensions + myOverdue + myDueSoon + myPending + undismissedDecisions + unreadQuotationNotifications,
      );
    };

    run().catch((err) => {
      // Belt-and-suspenders only -- every request above already has its
      // own .catch(), so this should never actually fire.
      console.error("Failed to compute notification count:", err);
    });

    return () => {
      cancelled = true;
    };
  }, [privileged]);

  return count;
}
