import { useEffect, useState } from "react";
import { Bell } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { alertsApi, extensionsApi, myItemsApi, quotationsApi } from "../lib/api";
import { useAuth } from "../lib/useAuth";
import { isPrivileged } from "../lib/roles";
import { readDismissedSet, readDismissedQuotationNotificationSet } from "../lib/notificationDismissals";

// =============================================================================
// components/NotificationBell.tsx
// -----------------------------------------------------------------------------
// Header bell -- a badge showing how many things need attention, linked
// straight into the full /notifications page (pages/Notifications.tsx).
// Tapping it always navigates there now; it used to open an inline dropdown
// with the same grouped content, but that content (including every "View ->"
// / "Request extension ->" click-through) now lives on that page instead, so
// there's a real, bookmarkable, full-width place for it rather than a cramped
// header popover. This component only needs to know the total COUNT for its
// badge, not the underlying rows.
// =============================================================================

export function NotificationBell() {
  const { user, demo } = useAuth();
  const navigate = useNavigate();
  const [count, setCount] = useState(0);
  const privileged = demo || isPrivileged(user?.role);

  useEffect(() => {
    let cancelled = false;
    const dismissed = readDismissedSet();
    const dismissedQuotationNotifications = readDismissedQuotationNotificationSet();

    const run = async () => {
      const [myItemsRes, decisions, overdue, dueSoon, extensions, quotationNotifications] = await Promise.all([
        myItemsApi.list().catch(() => ({ assigned_items: [] })),
        extensionsApi.myDecisions(10).catch(() => []),
        privileged ? alertsApi.overdue(5) : Promise.resolve({ items: [], total: 0 }),
        privileged ? alertsApi.dueSoon(5) : Promise.resolve({ items: [], total: 0 }),
        privileged ? extensionsApi.listPending().catch(() => []) : Promise.resolve([]),
        quotationsApi.myNotifications(),
      ]);
      if (cancelled) return;

      const items = myItemsRes.assigned_items ?? [];
      const myOverdue = items.filter((i) => i.overdue).length;
      const myDueSoon = items.filter((i) => i.due_soon && !i.overdue).length;
      const myPending = items.filter((i) => i.pending_extension).length;
      const undismissedDecisions = decisions.filter((d) => !dismissed.has(d.id)).length;
      const pendingExtensions = extensions.filter((e) => e.status === "pending").length;
      const unreadQuotationNotifications = quotationNotifications.filter(
        (n) => !n.read_at && !dismissedQuotationNotifications.has(n.id),
      ).length;

      setCount(overdue.total + dueSoon.total + pendingExtensions + myOverdue + myDueSoon + myPending + undismissedDecisions + unreadQuotationNotifications);
    };

    run();
    return () => {
      cancelled = true;
    };
  }, [privileged]);

  return (
    <button
      onClick={() => navigate("/notifications")}
      title="Notifications"
      className="relative p-1.5 rounded-[3px] hover:bg-surface transition-colors text-text-faint hover:text-text"
    >
      <Bell size={15} strokeWidth={1.75} />
      {count > 0 && (
        <span className="absolute -top-0.5 -right-0.5 min-w-[15px] h-[15px] px-[3px] rounded-full bg-brass text-ink text-[9px] font-bold flex items-center justify-center leading-none">
          {count > 99 ? "99+" : count}
        </span>
      )}
    </button>
  );
}
