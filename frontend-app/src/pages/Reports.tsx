import { useEffect, useMemo, useState } from "react";
import { motion } from "framer-motion";
import {
  BarChart, Bar, LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid,
} from "recharts";
import { Gauge, AlarmClockOff, Wallet, Clock3, Filter } from "lucide-react";
import { reportsApi, formatPrice } from "../lib/api";
import { StatCard } from "../components/StatCard";
import { TableShell, TableHead, TablePlaceholderRow } from "../components/ui/TableShell";
import { useTheme } from "../lib/useTheme";
import type { ReportsDashboard } from "../lib/types";

// recharts needs literal color strings, not the app's CSS custom
// properties -- same two palettes Dashboard.tsx already mirrors by hand.
const CHART_COLORS = {
  dark: { brass: "#C89B3C", sky: "#6B93C8", rust: "#B8613F", moss: "#6E9A6B", grid: "#232838", axis: "#5B6274", tooltipBg: "#1A1E28", tooltipBorder: "#2A2F3E" },
  light: { brass: "#966016", sky: "#2456A8", rust: "#9A4222", moss: "#3E7A3B", grid: "#E2E4F0", axis: "#8A8FA6", tooltipBg: "#FFFFFF", tooltipBorder: "#D6D9E6" },
};

function pct(rate: number | null): string {
  if (rate === null) return "—";
  return `${Math.round(rate * 1000) / 10}%`;
}

function hours(v: number | null): string {
  if (v === null) return "—";
  if (v < 48) return `${Math.round(v * 10) / 10}h`;
  return `${Math.round((v / 24) * 10) / 10}d`;
}

export function Reports() {
  const [data, setData] = useState<ReportsDashboard | null>(null);
  const [loading, setLoading] = useState(true);
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [category, setCategory] = useState("");
  const { theme } = useTheme();
  const chart = CHART_COLORS[theme];

  useEffect(() => {
    setLoading(true);
    reportsApi
      .dashboard(startDate || undefined, endDate || undefined, category || undefined)
      .then(setData)
      .catch((err) => console.error("Failed to load reports dashboard:", err))
      .finally(() => setLoading(false));
  }, [startDate, endDate, category]);

  const categories = useMemo(() => {
    const set = new Set<string>();
    for (const row of data?.utilization_by_asset_type ?? []) {
      if (row.category) set.add(row.category);
    }
    return Array.from(set).sort();
  }, [data]);

  const topUtilization = (data?.utilization_by_asset_type ?? []).slice(0, 8).map((r) => ({
    name: r.name.length > 18 ? `${r.name.slice(0, 17)}…` : r.name,
    utilization: r.utilization_rate !== null ? Math.round(r.utilization_rate * 1000) / 10 : 0,
  }));

  const totalSpend = (data?.spend.by_category ?? []).reduce((s, r) => s + r.total_spend, 0);

  return (
    <div>
      <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }} className="mb-6 flex items-start justify-between gap-3 flex-wrap">
        <div>
          <h1 className="font-display text-2xl font-semibold text-text">Reports</h1>
          <p className="text-text-muted text-sm mt-1">Utilization, overdue trends, spend, and quote turnaround across the fleet.</p>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <div className="flex items-center gap-1.5 text-text-faint">
            <Filter size={13} />
            <span className="text-[11px] uppercase tracking-wide">Filter</span>
          </div>
          <input
            type="date"
            value={startDate}
            onChange={(e) => setStartDate(e.target.value)}
            className="bg-surface border border-border-soft rounded-[3px] px-2.5 py-1.5 text-[12px] text-text focus:border-brass/50 focus:outline-none transition-colors"
            aria-label="Start date"
          />
          <span className="text-text-faint text-[12px]">to</span>
          <input
            type="date"
            value={endDate}
            onChange={(e) => setEndDate(e.target.value)}
            className="bg-surface border border-border-soft rounded-[3px] px-2.5 py-1.5 text-[12px] text-text focus:border-brass/50 focus:outline-none transition-colors"
            aria-label="End date"
          />
          {categories.length > 0 && (
            <select
              value={category}
              onChange={(e) => setCategory(e.target.value)}
              className="bg-surface border border-border-soft rounded-[3px] px-2.5 py-1.5 text-[12px] text-text focus:border-brass/50 focus:outline-none transition-colors"
              aria-label="Category filter (utilization)"
            >
              <option value="">All categories</option>
              {categories.map((c) => (
                <option key={c} value={c}>{c}</option>
              ))}
            </select>
          )}
          {(startDate || endDate || category) && (
            <button
              onClick={() => { setStartDate(""); setEndDate(""); setCategory(""); }}
              className="text-[11px] text-brass-soft hover:underline"
            >
              Clear
            </button>
          )}
        </div>
      </motion.div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-6">
        <StatCard index={0} label="Overdue right now" value={data?.overdue.total_overdue_now ?? "—"} icon={AlarmClockOff} accent="rust" hint="Active checkouts past due" to="/checkouts?tab=Overdue" />
        <StatCard index={1} label="Checked-out value" value={formatPrice(totalSpend)} icon={Wallet} accent="moss" hint={`${data?.spend.priced_checkout_count ?? 0} priced checkouts`} />
        <StatCard index={2} label="Avg. submit → fulfill" value={hours(data?.quotation_turnaround.avg_submit_to_fulfill_hours ?? null)} icon={Clock3} accent="sky" hint={`${data?.quotation_turnaround.sample_size_submit_to_fulfill ?? 0} completed quotes`} />
        <StatCard index={3} label="Highest utilization" value={pct(data?.utilization_by_asset_type[0]?.utilization_rate ?? null)} icon={Gauge} accent="brass" hint={data?.utilization_by_asset_type[0]?.name ?? "—"} />
      </div>

      {/* Utilization by asset type */}
      <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.45, delay: 0.1 }} className="border border-border-soft bg-surface rounded-[3px] p-5 mb-4">
        <div className="mb-4">
          <h2 className="font-display text-[15px] font-medium text-text">Utilization by asset type</h2>
          <p className="text-[11px] text-text-faint mt-0.5">Share of each pool currently checked out</p>
        </div>
        <ResponsiveContainer width="100%" height={260}>
          <BarChart data={topUtilization} margin={{ left: -20, right: 10 }}>
            <CartesianGrid stroke={chart.grid} vertical={false} />
            <XAxis dataKey="name" stroke={chart.axis} tick={{ fontSize: 10, fontFamily: "IBM Plex Mono" }} axisLine={false} tickLine={false} interval={0} angle={-20} textAnchor="end" height={50} />
            <YAxis stroke={chart.axis} tick={{ fontSize: 10, fontFamily: "IBM Plex Mono" }} axisLine={false} tickLine={false} width={36} unit="%" />
            <Tooltip
              contentStyle={{ background: chart.tooltipBg, border: `1px solid ${chart.tooltipBorder}`, borderRadius: 3, fontSize: 12 }}
              labelStyle={{ color: chart.axis }}
              formatter={(v) => [`${v}%`, "Utilization"]}
            />
            <Bar dataKey="utilization" fill={chart.brass} radius={[3, 3, 0, 0]} />
          </BarChart>
        </ResponsiveContainer>

        <TableShell>
          <table className="w-full text-[12.5px]">
            <TableHead>
              <th className="text-left px-4 py-2.5 font-medium">Asset type</th>
              <th className="text-left px-4 py-2.5 font-medium">Category</th>
              <th className="text-right px-4 py-2.5 font-medium">In use / total</th>
              <th className="text-right px-4 py-2.5 font-medium">Utilization</th>
              <th className="text-right px-4 py-2.5 font-medium">Checkouts</th>
            </TableHead>
            <tbody className="divide-y divide-border-soft">
              {loading && <TablePlaceholderRow columns={5}>Loading…</TablePlaceholderRow>}
              {!loading && (data?.utilization_by_asset_type.length ?? 0) === 0 && (
                <TablePlaceholderRow columns={5}>No asset pools yet.</TablePlaceholderRow>
              )}
              {!loading && data?.utilization_by_asset_type.map((row) => (
                <tr key={row.asset_type_id} className="text-text">
                  <td className="px-4 py-2.5">{row.name}</td>
                  <td className="px-4 py-2.5 text-text-muted">{row.category ?? "—"}</td>
                  <td className="px-4 py-2.5 text-right font-mono">{row.currently_checked_out} / {row.total_quantity}</td>
                  <td className="px-4 py-2.5 text-right font-mono">{pct(row.utilization_rate)}</td>
                  <td className="px-4 py-2.5 text-right font-mono text-text-muted">{row.checkout_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableShell>
      </motion.div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mb-4">
        {/* Overdue trend */}
        <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.45, delay: 0.15 }} className="border border-border-soft bg-surface rounded-[3px] p-5">
          <div className="mb-4">
            <h2 className="font-display text-[15px] font-medium text-text">Overdue trend</h2>
            <p className="text-[11px] text-text-faint mt-0.5">Checkouts that went overdue, by month due</p>
          </div>
          <ResponsiveContainer width="100%" height={200}>
            <LineChart data={data?.overdue.trend ?? []} margin={{ left: -20, right: 10 }}>
              <CartesianGrid stroke={chart.grid} vertical={false} />
              <XAxis dataKey="label" stroke={chart.axis} tick={{ fontSize: 10, fontFamily: "IBM Plex Mono" }} axisLine={false} tickLine={false} />
              <YAxis stroke={chart.axis} tick={{ fontSize: 10, fontFamily: "IBM Plex Mono" }} axisLine={false} tickLine={false} width={24} allowDecimals={false} />
              <Tooltip contentStyle={{ background: chart.tooltipBg, border: `1px solid ${chart.tooltipBorder}`, borderRadius: 3, fontSize: 12 }} labelStyle={{ color: chart.axis }} />
              <Line type="monotone" dataKey="overdue_count" name="Overdue" stroke={chart.rust} strokeWidth={2} dot={{ r: 3 }} />
            </LineChart>
          </ResponsiveContainer>

          {(data?.overdue.by_department.length ?? 0) > 0 && (
            <div className="mt-4 pt-4 border-t border-border-soft">
              <p className="text-[11px] uppercase tracking-wider text-text-faint mb-2">Overdue now, by department</p>
              <div className="flex flex-col divide-y divide-border-soft">
                {data?.overdue.by_department.slice(0, 6).map((row) => (
                  <div key={row.department} className="flex items-center justify-between py-1.5 text-[12.5px]">
                    <span className="text-text">{row.department}</span>
                    <span className="font-mono text-text-muted">{row.overdue_count}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </motion.div>

        {/* Quotation turnaround */}
        <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.45, delay: 0.2 }} className="border border-border-soft bg-surface rounded-[3px] p-5">
          <div className="mb-4">
            <h2 className="font-display text-[15px] font-medium text-text">Quotation approval turnaround</h2>
            <p className="text-[11px] text-text-faint mt-0.5">Average hours, submit → fulfill, by month submitted</p>
          </div>
          <ResponsiveContainer width="100%" height={200}>
            <LineChart data={data?.quotation_turnaround.by_month ?? []} margin={{ left: -20, right: 10 }}>
              <CartesianGrid stroke={chart.grid} vertical={false} />
              <XAxis dataKey="label" stroke={chart.axis} tick={{ fontSize: 10, fontFamily: "IBM Plex Mono" }} axisLine={false} tickLine={false} />
              <YAxis stroke={chart.axis} tick={{ fontSize: 10, fontFamily: "IBM Plex Mono" }} axisLine={false} tickLine={false} width={30} />
              <Tooltip contentStyle={{ background: chart.tooltipBg, border: `1px solid ${chart.tooltipBorder}`, borderRadius: 3, fontSize: 12 }} labelStyle={{ color: chart.axis }} />
              <Line type="monotone" dataKey="avg_submit_to_fulfill_hours" name="Avg hours" stroke={chart.sky} strokeWidth={2} dot={{ r: 3 }} connectNulls />
            </LineChart>
          </ResponsiveContainer>

          <div className="mt-4 pt-4 border-t border-border-soft grid grid-cols-2 gap-3 text-[12.5px]">
            <div>
              <p className="text-text-faint text-[11px] uppercase tracking-wide">Submit → approve</p>
              <p className="text-text font-mono mt-0.5">{hours(data?.quotation_turnaround.avg_submit_to_approve_hours ?? null)}</p>
            </div>
            <div>
              <p className="text-text-faint text-[11px] uppercase tracking-wide">Approve → fulfill</p>
              <p className="text-text font-mono mt-0.5">{hours(data?.quotation_turnaround.avg_approve_to_fulfill_hours ?? null)}</p>
            </div>
          </div>
        </motion.div>
      </div>

      {/* Spend by category / department */}
      <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.45, delay: 0.25 }} className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="border border-border-soft bg-surface rounded-[3px] p-5">
          <h2 className="font-display text-[15px] font-medium text-text mb-3">Spend by category</h2>
          <div className="flex flex-col divide-y divide-border-soft">
            {(data?.spend.by_category.length ?? 0) === 0 && <p className="text-[12px] text-text-faint py-6 text-center">Nothing priced in this window.</p>}
            {data?.spend.by_category.map((row) => (
              <div key={row.category} className="flex items-center justify-between py-2 text-[12.5px]">
                <div>
                  <p className="text-text">{row.category}</p>
                  <p className="text-text-faint text-[10.5px]">{row.item_count} unit(s)</p>
                </div>
                <span className="font-mono text-text">{formatPrice(row.total_spend)}</span>
              </div>
            ))}
          </div>
        </div>
        <div className="border border-border-soft bg-surface rounded-[3px] p-5">
          <h2 className="font-display text-[15px] font-medium text-text mb-3">Spend by department</h2>
          <div className="flex flex-col divide-y divide-border-soft">
            {(data?.spend.by_department.length ?? 0) === 0 && <p className="text-[12px] text-text-faint py-6 text-center">Nothing priced in this window.</p>}
            {data?.spend.by_department.map((row) => (
              <div key={row.department} className="flex items-center justify-between py-2 text-[12.5px]">
                <div>
                  <p className="text-text">{row.department}</p>
                  <p className="text-text-faint text-[10.5px]">{row.item_count} unit(s)</p>
                </div>
                <span className="font-mono text-text">{formatPrice(row.total_spend)}</span>
              </div>
            ))}
          </div>
        </div>
      </motion.div>
    </div>
  );
}
