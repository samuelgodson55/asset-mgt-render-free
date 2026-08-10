import { Bell } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../lib/useAuth";
import { isPrivileged } from "../lib/roles";
import { useNotificationCount } from "../lib/useNotificationCount";

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
// badge, not the underlying rows -- see lib/useNotificationCount.ts for that
// shared calculation (also used by the sidebar "Notifications" nav badge in
// components/Layout.tsx, so the two numbers can never drift apart).
// =============================================================================

export function NotificationBell() {
  const { user, demo } = useAuth();
  const navigate = useNavigate();
  const privileged = demo || isPrivileged(user?.role);
  const count = useNotificationCount(privileged);

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
