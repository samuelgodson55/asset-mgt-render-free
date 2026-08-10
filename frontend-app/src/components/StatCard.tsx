import { motion } from "framer-motion";
import { Link } from "react-router-dom";
import { ArrowUpRight, type LucideIcon } from "lucide-react";

interface Props {
  label: string;
  value: string | number;
  icon: LucideIcon;
  accent?: "brass" | "moss" | "rust" | "sky";
  hint?: string;
  index?: number;
  /** Where this card should take the person when clicked -- e.g. "Overdue
   * returns" goes straight to /checkouts pre-filtered to the Overdue tab
   * rather than leaving them to go find and re-filter it themselves. Cards
   * without a destination (nothing to drill into) just render inert, same
   * as before. */
  to?: string;
}

const accentMap = {
  brass: "text-brass-soft",
  moss: "text-moss-soft",
  rust: "text-rust-soft",
  sky: "text-sky",
};

export function StatCard({ label, value, icon: Icon, accent = "brass", hint, index = 0, to }: Props) {
  const content = (
    <>
      <div className="flex items-start justify-between">
        <p className="text-[11px] uppercase tracking-wider text-text-faint">{label}</p>
        <Icon size={14} strokeWidth={1.75} className={accentMap[accent]} />
      </div>
      <p className="font-display text-[26px] font-semibold text-text mt-2 leading-none">{value}</p>
      {hint && (
        <p className="text-[11px] text-text-muted mt-1.5 flex items-center gap-1">
          {hint}
          {to && (
            <ArrowUpRight
              size={11}
              className="text-text-faint opacity-0 -translate-x-0.5 group-hover:opacity-100 group-hover:translate-x-0 transition-all duration-200"
            />
          )}
        </p>
      )}
    </>
  );

  const className =
    "group block w-full text-left border border-border-soft bg-surface rounded-[3px] p-4 relative overflow-hidden" +
    (to ? " cursor-pointer transition-colors hover:border-brass/40 hover:bg-surface-raised" : "");

  if (to) {
    return (
      <motion.div
        initial={{ opacity: 0, y: 10 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4, delay: index * 0.06, ease: [0.16, 1, 0.3, 1] }}
      >
        <Link to={to} className={className}>
          {content}
        </Link>
      </motion.div>
    );
  }

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4, delay: index * 0.06, ease: [0.16, 1, 0.3, 1] }}
      className={className}
    >
      {content}
    </motion.div>
  );
}
