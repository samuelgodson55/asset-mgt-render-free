import { useEffect, useState } from "react";
import { motion } from "framer-motion";
import { AlarmClockOff, CalendarClock, Info, TrendingDown } from "lucide-react";
import { api, relativeTime } from "../lib/api";
import type { NotificationItem } from "../lib/types";

const iconMap = {
  overdue: { icon: AlarmClockOff, color: "text-rust-soft bg-rust/10" },
  extension: { icon: CalendarClock, color: "text-sky bg-sky/10" },
  system: { icon: Info, color: "text-text-muted bg-surface-raised" },
  low_stock: { icon: TrendingDown, color: "text-brass-soft bg-brass/10" },
};

export function Notifications() {
  const [items, setItems] = useState<NotificationItem[]>([]);

  useEffect(() => {
    api.getNotifications().then(setItems);
  }, []);

  return (
    <div>
      <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }} className="mb-6">
        <h1 className="font-display text-2xl font-semibold text-text">Notifications</h1>
        <p className="text-text-muted text-sm mt-1">{items.filter((i) => !i.read).length} unread</p>
      </motion.div>

      <div className="max-w-xl flex flex-col gap-2">
        {items.map((n, i) => {
          const { icon: Icon, color } = iconMap[n.kind];
          return (
            <motion.div
              key={n.id}
              initial={{ opacity: 0, x: -8 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ duration: 0.3, delay: i * 0.05 }}
              className={`flex gap-3 p-4 rounded-[3px] border ${n.read ? "border-border-soft bg-surface" : "border-brass/30 bg-surface-raised"}`}
            >
              <div className={`w-8 h-8 rounded-full flex items-center justify-center shrink-0 ${color}`}>
                <Icon size={14} strokeWidth={1.75} />
              </div>
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <p className="text-[13px] text-text font-medium">{n.title}</p>
                  {!n.read && <span className="w-1.5 h-1.5 rounded-full bg-brass shrink-0" />}
                </div>
                <p className="text-[12px] text-text-muted mt-0.5">{n.body}</p>
                <p className="text-[10.5px] text-text-faint mt-1.5 font-mono">{relativeTime(n.created_at)}</p>
              </div>
            </motion.div>
          );
        })}
      </div>
    </div>
  );
}
