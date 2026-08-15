import { useEffect, useState } from "react";
import { motion } from "framer-motion";
import { AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from "recharts";
import { Boxes, PackageCheck, AlarmClockOff, TrendingDown, ArrowRight } from "lucide-react";
import { api, myItemsApi, relativeTime, formatDate } from "../lib/api";
import { StatCard } from "../components/StatCard";
import { StatusPill } from "../components/StatusPill";
import type { Checkout, DashboardStats, MyItem } from "../lib/types";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "../lib/useAuth";
import { useTheme } from "../lib/useTheme";
import { isPrivileged } from "../lib/roles";
import { useRequestGuard } from "../lib/useRequestGuard";
import { useCustody } from "../lib/useCustody";

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
  const [myItemsLoaded, setMyItemsLoaded] = useState(false);
  const { user, demo, canSeeStock } = useAuth();
  const { theme } = useTheme();
  const navigate = useNavigate();
  // Same click-through the Notification Bell's grouped rows and
  // Checkouts.tsx's rows now use: a "Needs attention" row IS a person
  // holding something overdue/due-soon, so clicking it should jump
  // straight to their Custody Ledger rather than going nowhere.
  const { openCustody } = useCustody();
  const chart = CHART_COLORS[theme];
  const firstName = user?.name?.trim().split(/\s+/)[0];
  // Overdue/due-soon checkouts (system-wide) and the extension-request
  // review queue are require_privileged_role on the backend -- a Staff/
  // Customer session sees their OWN items in "Needs attention" instead
  // (see lib/api.ts's loadStats()/loadCheckouts() for why this can't just
  // attempt the privileged endpoints and let a 403 fall through to mock
  // data).
  const privileged = demo || isPrivileged(user?.role);
  const beginRequest = useRequestGuard();

  useEffect(() => {
    const isCurrent = beginRequest();
    api.getStats(privileged).then((data) => { if (isCurrent()) setStats(data); }).catch((err) => { if (isCurrent()) console.error("Failed to load dashboard stats:", err); });
    if (privileged) {
      api.getCheckouts(privileged).then((data) => { if (isCurrent()) setCheckouts(data); }).catch((err) => { if (isCurrent()) console.error("Failed to load checkouts:", err); });
    } else {
      myItemsApi.list()
        .then((d) => { if (!isCurrent()) return; setMyItems(d.assigned_items); setMyItemsLoaded(true); })
        .catch(() => { if (!isCurrent()) return; setMyItems([]); setMyItemsLoaded(true); });
    }
  }, [privileged, beginRequest]);

  const attention = privileged
    ? checkouts
        .filter((c) => c.status === "overdue" || c.due_soon)
        .sort((a, b) => new Date(a.due_at).getTime() - new Date(b.due_at).getTime())
        .slice(0, 5)
    : [];

  const myAttention = privileged
    ? []
    : myItems
        .filter((i) => i.overdue || i.due_soon)
        .sort((a, b) => new Date(a.due_date).getTime() - new Date(b.due_date).getTime())
        .slice(0, 5);

  // Own-custody total (sum of quantity across every item currently
  // checked out to this person, from GET /users/me/items) -- stands in
  // for the "Total pooled units" card when the signed-in role can't see
  // org-wide stock (see canSeeStock's docstring in lib/roles.ts): that
  // card sources total_assets from GET /assets' total_quantity, which the
  // backend omits entirely for a Staff/Customer session (asset_service.py's
  // _serialize_asset_type()), so it always read 0 there even when this
  // person plainly has items checked out -- the bug being fixed here.
  const myCheckedOutTotal = myItems.reduce((sum, i) => sum + i.quantity, 0);

  // Same fix, applied to "Fleet by category" below: stats.categories is
  // built server-side from GET /assets' total_quantity too (see
  // lib/api.ts's loadStats()), so it's just as empty for a Staff/Customer
  // session. Group this person's OWN items by asset_category instead --
  // same "—" fallback the CSV/PDF export already uses for an uncategorized
  // pool (services/user_service.py's export helpers).
  const myCategoryCounts: { name: string; count: number }[] = (() => {
    const byCategory = new Map<string, number>();
    for (const i of myItems) {
      const key = i.asset_category ?? "Uncategorized";
      byCategory.set(key, (byCategory.get(key) ?? 0) + i.quantity);
    }
    return Array.from(byCategory, ([name, count]) => ({ name, count }));
  })();

  return (
    <div>
      <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }} className="mb-6">
        <p className="font-mono text-[11px] text-brass-soft tracking-widest">{formatDate(new Date().toISOString())}</p>
        <h1 className="font-display text-2xl font-semibold text-text mt-1">Good to see you{firstName ? `, ${firstName}` : ""}.</h1>
        <p className="text-text-muted text-sm mt-1">Here's what the ledger looks like this morning.</p>
      </motion.div>

      {/* "Total pooled units", "Available now", and "Low stock pools" are
          all org-wide STOCK numbers -- gated behind canSeeStock the same
          way the Quotation Catalog already gates available_quantity/status
          for a Staff/Customer session (see lib/roles.ts's canSeeStock()).
          GET /assets omits total_quantity/available_quantity entirely for
          that role (asset_service.py's _serialize_asset_type()), so
          "Total pooled units" always read a misleading 0 there -- even
          for someone who plainly has items checked out -- instead of
          reflecting a number they were never allowed to see in the first
          place. It's swapped for that person's OWN checked-out total
          (from GET /users/me/items, open to everyone) so the card always
          shows something true. Grid columns adapt to however many cards
          actually render so a Staff/Customer session without the flag
          doesn't end up with lopsided empty space. */}
      <div className={`grid grid-cols-2 gap-3 mb-6 ${canSeeStock ? "md:grid-cols-4" : "md:grid-cols-2"}`}>
        {canSeeStock ? (
          <StatCard index={0} label="Total pooled units" value={stats?.total_assets ?? "—"} icon={Boxes} accent="sky" hint="Across all categories" to="/assets" />
        ) : (
          <StatCard index={0} label="Your checked-out items" value={myItemsLoaded ? myCheckedOutTotal : "—"} icon={PackageCheck} accent="sky" hint="Currently in your custody" to="/my-items" />
        )}
        {canSeeStock && (
          <StatCard index={1} label="Available now" value={stats?.available ?? "—"} icon={PackageCheck} accent="moss" hint="Ready to check out" to="/assets?status=available" />
        )}
        <StatCard
          index={2}
          label={privileged ? "Overdue returns" : "Your overdue items"}
          value={stats?.overdue ?? "—"}
          icon={AlarmClockOff}
          accent="rust"
          hint={privileged ? "Needs follow-up" : "Return these first"}
          to={privileged ? "/checkouts?tab=Overdue" : "/my-items?filter=overdue"}
        />
        {canSeeStock && (
          <StatCard index={3} label="Low stock pools" value={stats?.low_stock ?? "—"} icon={TrendingDown} accent="brass" hint="Below 25% available" to="/assets?status=low" />
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
              <h2 className="font-display text-[15px] font-medium text-text">{privileged ? "Checkout activity" : "Your checkout activity"}</h2>
              <p className="text-[11px] text-text-faint mt-0.5">Last 14 days{privileged ? "" : " · your items only"}</p>
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
              {attention.map((c) => {
                const clickable = c.entity_id != null;
                return (
                  <button
                    key={c.id}
                    onClick={clickable ? () => openCustody(c.entity_type ?? "user", c.entity_id as number, c.checked_out_to) : undefined}
                    disabled={!clickable}
                    title={clickable ? `View ${c.checked_out_to}'s Custody Ledger` : undefined}
                    className={`w-full py-2.5 flex items-center justify-between gap-3 text-left rounded-[3px] transition-colors -mx-1.5 px-1.5 ${
                      clickable ? "hover:bg-surface-raised cursor-pointer" : "cursor-default"
                    }`}
                  >
                    <div className="min-w-0">
                      <p className="text-[12.5px] text-text truncate">{c.asset_name}</p>
                      <p className="text-[11px] text-text-faint font-mono truncate">{c.tag} · {c.checked_out_to}</p>
                    </div>
                    <div className="text-right shrink-0">
                      <StatusPill status={c.status === "overdue" ? "overdue" : "active"} />
                      <p className="text-[10px] text-text-faint mt-0.5">{relativeTime(c.due_at)}</p>
                    </div>
                  </button>
                );
              })}
            </div>
          ) : (
            <div className="flex flex-col divide-y divide-border-soft">
              {myAttention.length === 0 && <p className="text-[12px] text-text-faint py-6 text-center">Nothing of yours is overdue or due soon.</p>}
              {myAttention.map((i) => (
                <button
                  key={i.checkout_id}
                  onClick={() => navigate(`/my-items?filter=${i.overdue ? "overdue" : "due_soon"}`)}
                  title="Go to My Items"
                  className="w-full py-2.5 flex items-center justify-between gap-3 text-left rounded-[3px] hover:bg-surface-raised transition-colors cursor-pointer -mx-1.5 px-1.5"
                >
                  <div className="min-w-0">
                    <p className="text-[12.5px] text-text truncate">{i.asset_name}</p>
                    <p className="text-[11px] text-text-faint font-mono truncate">qty {i.quantity}</p>
                  </div>
                  <div className="text-right shrink-0">
                    <StatusPill status={i.overdue ? "overdue" : "active"} />
                    <p className="text-[10px] text-text-faint mt-0.5">{relativeTime(i.due_date)}</p>
                  </div>
                </button>
              ))}
            </div>
          )}
        </motion.div>
      </div>

      {(canSeeStock || myCategoryCounts.length > 0) && (
        <motion.div
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.45, delay: 0.28 }}
          className="border border-border-soft bg-surface rounded-[3px] p-5 mt-4"
        >
          <h2 className="font-display text-[15px] font-medium text-text mb-4">{canSeeStock ? "Fleet by category" : "Your items by category"}</h2>
          <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
            {(canSeeStock ? stats?.categories ?? [] : myCategoryCounts).map((c, i) =>
              canSeeStock ? (
                <motion.div
                  key={c.name}
                  initial={{ opacity: 0, scale: 0.96 }}
                  animate={{ opacity: 1, scale: 1 }}
                  transition={{ duration: 0.3, delay: 0.3 + i * 0.04 }}
                >
                  <Link
                    to={`/assets?category=${encodeURIComponent(c.name)}`}
                    className="group block border border-border-soft rounded-[3px] p-3 hover:border-brass/40 hover:bg-surface-raised transition-colors"
                  >
                    <p className="text-[11px] text-text-muted truncate group-hover:text-text-faint">{c.name}</p>
                    <p className="font-mono text-lg text-text mt-1">{c.count}</p>
                  </Link>
                </motion.div>
              ) : (
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
              )
            )}
          </div>
        </motion.div>
      )}
    </div>
  );
}
