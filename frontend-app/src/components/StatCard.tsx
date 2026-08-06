import { motion } from "framer-motion";
import type { LucideIcon } from "lucide-react";

interface Props {
  label: string;
  value: string | number;
  icon: LucideIcon;
  accent?: "brass" | "moss" | "rust" | "sky";
  hint?: string;
  index?: number;
}

const accentMap = {
  brass: "text-brass-soft",
  moss: "text-moss-soft",
  rust: "text-rust-soft",
  sky: "text-sky",
};

export function StatCard({ label, value, icon: Icon, accent = "brass", hint, index = 0 }: Props) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4, delay: index * 0.06, ease: [0.16, 1, 0.3, 1] }}
      className="border border-border-soft bg-surface rounded-[3px] p-4 relative overflow-hidden"
    >
      <div className="flex items-start justify-between">
        <p className="text-[11px] uppercase tracking-wider text-text-faint">{label}</p>
        <Icon size={14} strokeWidth={1.75} className={accentMap[accent]} />
      </div>
      <p className="font-display text-[26px] font-semibold text-text mt-2 leading-none">{value}</p>
      {hint && <p className="text-[11px] text-text-muted mt-1.5">{hint}</p>}
    </motion.div>
  );
}
