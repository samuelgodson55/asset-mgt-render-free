import { useEffect, useState } from "react";
import { motion } from "framer-motion";
import { AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from "recharts";
import { Boxes, PackageCheck, AlarmClockOff, TrendingDown, ArrowRight } from "lucide-react";
import { api, myItemsApi, relativeTime, formatDate } from "../lib/api";
import { StatCard } from "../components/StatCard";
import { StatusPill } from "../components/StatusPill";
import type { Checkout, DashboardStats, MyItem } from "../lib/types";
import { Link } from "react-router-dom";
import { useAuth } from "../lib/useAuth";
import { useTheme } from "../lib/useTheme";
import { isPrivileged } from "../lib/roles";

// recharts needs literal color strings (SVG attrs, not the app's CSS
// custom properties), so the two palettes are mirrored here by hand.
const CHART_COLORS = {
  dark: { brass: "#C89B3C", sky: "#6B93C8", grid: "#232838", axis: "#5B6274", tooltipBg: "#1A1E28", tooltipBorder: "#2A2F3E" },
  light: { brass: "#966016", sky: "#2456A8", grid: "#E2E4F0", axis: "#8A8FA6", tooltipBg: "#FFFFFF", tooltipBorder: "#D6D9E6" },
};

export function Dashboard() {
  const [stats, setStats] = useState<DashboardStats | null>(null);
  const [checkouts, setCheckouts] = useState<Checkout[]>([]);
  const [myItems, setMyItems] = useState<MyItem[]>([]);
  const { user, demo, canSeeStock } = useAuth();
  const { theme } = useTheme();
  const chart = CHART_COLORS[theme];
  const firstName = user?.name?.trim().split(/\s+/)[0];
  // Overdue/due-soon checkouts (system-wide) and the extension-request
  // review queue are require_privileged_role on the backend -- a Staff/
  // Customer session sees their OWN items in "Needs attention" instead
  // (see lib/api.ts's loadStats()/loadCheckouts() for why this can't just
  // attempt the privileged endpoints and let a 403 fall through to mock
  // data).
  const privileged = demo || isPrivileged(user?.role);

  useEffect(() => {
    api.getStats(privileged).then(setStats);
    if (privileged) {
      api.getCheckouts(privileged).then(setCheckouts);
    } else {
      myItemsApi.list().then((d) => setMyItems(d.assigned_items)).catch(() => setMyItems([]));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [privileged]);

  const attention = privileged
    ? checkouts
        .filter((c) => c.status === "overdue" || (new Date(c.due_at).getTime() - Date.now()) / 86400000 <= 2)
        .sort((a, b) => new Date(a.due_at).getTime() - new Date(b.due_at).getTime())
        .slice(0, 5)
    : [];

  const myAttention = privileged
    ? []
    : myItems
        .filter((i) => i.overdue || i.due_soon)
        .sort((a, b) => new Date(a.due_date).getTime() - new Date(b.due_date).getTime())
        .slice(0, 5);

  return (
    <div>
      <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }} className="mb-6">
        <p className="font-mono text-[11px] text-brass-soft tracking-widest">{formatDate(new Date().toISOString())}</p>
        <h1 className="font-display text-2xl font-semibold text-text mt-1">Good to see you{firstName ? `, ${firstName}` : ""}.</h1>
        <p className="text-text-muted text-sm mt-1">Here's what the ledger looks like this morning.</p>
      </motion.div>

      {/* "Available now" and "Low stock pools" both surface real-time stock
          levels -- gated behind canSeeStock the same way the Quotation
          Catalog already gates available_quantity/status for a Staff/
          Customer session (see lib/roles.ts's canSeeStock()). Grid columns
          adapt to however many cards actually render so a Staff/Customer
          session without the flag doesn't end up with lopsided empty
          space. */}
      <div className={`grid grid-cols-2 gap-3 mb-6 ${canSeeStock ? "md:grid-cols-4" : "md:grid-cols-2"}`}>
        <StatCard index={0} label="Total pooled units" value={stats?.total_assets ?? "—"} icon={Boxes} accent="sky" hint="Across all categories" />
        {canSeeStock && (
          <StatCard index={1} label="Available now" value={stats?.available ?? "—"} icon={PackageCheck} accent="moss" hint="Ready to check out" />
        )}
        <StatCard index={2} label={privileged ? "Overdue returns" : "Your overdue items"} value={stats?.overdue ?? "—"} icon={AlarmClockOff} accent="rust" hint={privileged ? "Needs follow-up" : "Return these first"} />
        {canSeeStock && (
          <StatCard index={3} label="Low stock pools" value={stats?.low_stock ?? "—"} icon={TrendingDown} accent="brass" hint="Below 25% available" />
        )}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-5 gap-4">
        <motion.div
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.45, delay: 0.15 }}
          className="lg:col-span-3 border border-border-soft bg-surface rounded-[3px] p-5"
        >
          <div className="flex items-center justify-between mb-4">
            <div>
              <h2 className="font-display text-[15px] font-medium text-text">Checkout activity</h2>
              <p className="text-[11px] text-text-faint mt-0.5">Last 14 days</p>
            </div>
            <div className="flex items-center gap-3 text-[11px] text-text-muted">
              <span className="flex items-center gap-1.5"><span className="w-2 h-2 rounded-full bg-brass inline-block" />Checkouts</span>
              <span className="flex items-center gap-1.5"><span className="w-2 h-2 rounded-full bg-sky inline-block" />Returns</span>
            </div>
          </div>
          <ResponsiveContainer width="100%" height={220}>
            <AreaChart data={stats?.activity ?? []} margin={{ left: -20, right: 10 }}>
              <defs>
                <linearGradient id="checkoutGrad" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={chart.brass} stopOpacity={0.35} />
                  <stop offset="100%" stopColor={chart.brass} stopOpacity={0} />
                </linearGradient>
                <linearGradient id="returnGrad" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={chart.sky} stopOpacity={0.3} />
                  <stop offset="100%" stopColor={chart.sky} stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid stroke={chart.grid} vertical={false} />
              <XAxis dataKey="date" stroke={chart.axis} tick={{ fontSize: 10, fontFamily: "IBM Plex Mono" }} axisLine={false} tickLine={false} />
              <YAxis stroke={chart.axis} tick={{ fontSize: 10, fontFamily: "IBM Plex Mono" }} axisLine={false} tickLine={false} width={24} />
              <Tooltip
                contentStyle={{ background: chart.tooltipBg, border: `1px solid ${chart.tooltipBorder}`, borderRadius: 3, fontSize: 12 }}
                labelStyle={{ color: chart.axis }}
              />
              <Area type="monotone" dataKey="checkouts" stroke={chart.brass} strokeWidth={2} fill="url(#checkoutGrad)" />
              <Area type="monotone" dataKey="returns" stroke={chart.sky} strokeWidth={2} fill="url(#returnGrad)" />
            </AreaChart>
          </ResponsiveContainer>
        </motion.div>

        <motion.div
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.45, delay: 0.2 }}
          className="lg:col-span-2 border border-border-soft bg-surface rounded-[3px] p-5 flex flex-col"
        >
          <div className="flex items-center justify-between mb-3">
            <h2 className="font-display text-[15px] font-medium text-text">{privileged ? "Needs attention" : "Your items needing attention"}</h2>
            {privileged && (
              <Link to="/checkouts" className="text-[11px] text-brass-soft flex items-center gap-1 hover:gap-1.5 transition-all">
                View all <ArrowRight size={11} />
              </Link>
            )}
            {!privileged && (
              <Link to="/my-items" className="text-[11px] text-brass-soft flex items-center gap-1 hover:gap-1.5 transition-all">
                My Items <ArrowRight size={11} />
              </Link>
            )}
          </div>
          {privileged ? (
            <div className="flex flex-col divide-y divide-border-soft">
              {attention.length === 0 && <p className="text-[12px] text-text-faint py-6 text-center">Nothing overdue. Ledger's clean.</p>}
              {attention.map((c) => (
                <div key={c.id} className="py-2.5 flex items-center justify-between gap-3">
                  <div className="min-w-0">
                    <p className="text-[12.5px] text-text truncate">{c.asset_name}</p>
                    <p className="text-[11px] text-text-faint font-mono truncate">{c.tag} · {c.checked_out_to}</p>
                  </div>
                  <div className="text-right shrink-0">
                    <StatusPill status={c.status === "overdue" ? "overdue" : "active"} />
                    <p className="text-[10px] text-text-faint mt-0.5">{relativeTime(c.due_at)}</p>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="flex flex-col divide-y divide-border-soft">
              {myAttention.length === 0 && <p className="text-[12px] text-text-faint py-6 text-center">Nothing of yours is overdue or due soon.</p>}
              {myAttention.map((i) => (
                <div key={i.checkout_id} className="py-2.5 flex items-center justify-between gap-3">
                  <div className="min-w-0">
                    <p className="text-[12.5px] text-text truncate">{i.asset_name}</p>
                    <p className="text-[11px] text-text-faint font-mono truncate">qty {i.quantity}</p>
                  </div>
                  <div className="text-right shrink-0">
                    <StatusPill status={i.overdue ? "overdue" : "active"} />
                    <p className="text-[10px] text-text-faint mt-0.5">{relativeTime(i.due_date)}</p>
                  </div>
                </div>
              ))}
            </div>
          )}
        </motion.div>
      </div>

      <motion.div
        initial={{ opacity: 0, y: 10 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.45, delay: 0.28 }}
        className="border border-border-soft bg-surface rounded-[3px] p-5 mt-4"
      >
        <h2 className="font-display text-[15px] font-medium text-text mb-4">Fleet by category</h2>
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
          {stats?.categories.map((c, i) => (
            <motion.div
              key={c.name}
              initial={{ opacity: 0, scale: 0.96 }}
              animate={{ opacity: 1, scale: 1 }}
              transition={{ duration: 0.3, delay: 0.3 + i * 0.04 }}
              className="border border-border-soft rounded-[3px] p-3"
            >
              <p className="text-[11px] text-text-muted truncate">{c.name}</p>
              <p className="font-mono text-lg text-text mt-1">{c.count}</p>
            </motion.div>
          ))}
        </div>
      </motion.div>
    </div>
  );
}
