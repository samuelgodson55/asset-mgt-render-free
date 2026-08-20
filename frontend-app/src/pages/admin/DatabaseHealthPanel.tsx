import { useCallback, useEffect, useMemo, useState } from "react";
import { motion } from "framer-motion";
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  CircleHelp,
  Database,
  Gauge,
  HardDrive,
  Loader2,
  Network,
  RefreshCw,
  Server,
  ShieldCheck,
  Timer,
  Waves,
  XCircle,
} from "lucide-react";
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { diagnosticsApi } from "../../lib/api";
import { useAuth } from "../../lib/useAuth";
import { useRequestGuard } from "../../lib/useRequestGuard";
import { ApiError } from "../../lib/api";
import type { ReactNode } from "react";
import { ErrorBanner } from "../../components/ui/ErrorBanner";

import type { DbPoolDiagnostics } from "../../lib/types";

type HealthLevel = "healthy" | "attention" | "critical" | "unknown";

const DEMO_SNAPSHOT: DbPoolDiagnostics = {
  database_route: { configured: true, in_use: true, host: "pgbouncer.internal", port: 6432, expected_pooler_host: "pgbouncer.internal", expected_pooler_port: 6432 },
  sqlalchemy_pool: { pool_size: 10, checked_out: 3, checked_in: 7, overflow: 0 },
  pgbouncer_pool: { reachable: true, in_use: true, cl_active: 3, cl_waiting: 0, sv_active: 3, sv_idle: 7, sv_used: 3, maxwait_seconds: 0, avg_query_time_us: 12000, avg_wait_time_us: 0, pool_mode: "transaction", max_client_conn: 200, default_pool_size: 12, reserve_pool_size: 4 },
  postgres_activity: { max_connections: 100, total_connections: 18, active: 4, idle: 14, idle_in_transaction: 0 },
  configured: { use_pgbouncer: true, pgbouncer_server_pool_size: 12, pgbouncer_safety_margin_percent: 20, db_background_connection_reserve: 2, db_background_concurrency_limit: 2, db_connection_safety_margin: 10 },
};

function pct(value: number, max: number) {
  return max > 0 ? Math.min(100, Math.max(0, (value / max) * 100)) : 0;
}

function formatDurationUs(value?: number) {
  if (value == null) return "—";
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(2)} s`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)} ms`;
  return `${value} µs`;
}

function healthFor(snapshot: DbPoolDiagnostics | null): { level: HealthLevel; label: string; detail: string } {
  if (!snapshot) return { level: "unknown", label: "No data", detail: "Waiting for a live database snapshot." };
  const pg = snapshot.postgres_activity;
  const pool = snapshot.sqlalchemy_pool;
  const b = snapshot.pgbouncer_pool;
  const route = snapshot.database_route;
  if (!pg || !pool) return { level: "unknown", label: "Partial data", detail: "Some probes are unavailable; check connectivity before tuning capacity." };
  const routeMismatch = !!route?.configured && !route?.in_use;
  const bouncerUnreachable = !!route?.configured && b != null && b.reachable === false;
  const postgresPct = pct(pg.total_connections ?? 0, pg.max_connections ?? 0);
  const critical = postgresPct >= 90 || (b?.cl_waiting ?? 0) >= 3 || (pool.overflow ?? 0) >= 3 || (pg.idle_in_transaction ?? 0) >= 3;
  const attention = postgresPct >= 75 || (b?.cl_waiting ?? 0) > 0 || (pool.overflow ?? 0) > 0 || (pg.idle_in_transaction ?? 0) > 0 || (b?.avg_wait_time_us ?? 0) > 0 || routeMismatch || bouncerUnreachable;
  if (critical) return { level: "critical", label: "Critical", detail: "Connection pressure is high. Investigate before increasing traffic or pool limits." };
  if (routeMismatch) return { level: "attention", label: "Attention", detail: "PgBouncer is configured but the API isn't actually routed through it right now." };
  if (bouncerUnreachable) return { level: "attention", label: "Attention", detail: "PgBouncer's admin console couldn't be reached for a live probe." };
  if (attention) return { level: "attention", label: "Attention", detail: "The database is showing a pressure signal worth watching." };
  return { level: "healthy", label: "Healthy", detail: "Connection pools have headroom and no clients are waiting." };
}

function StatusIcon({ level }: { level: HealthLevel }) {
  if (level === "healthy") return <CheckCircle2 size={17} />;
  if (level === "attention") return <AlertTriangle size={17} />;
  if (level === "critical") return <XCircle size={17} />;
  return <CircleHelp size={17} />;
}

function MetricCard({ label, value, caption, tone = "neutral", icon }: { label: string; value: string | number; caption: string; tone?: "neutral" | "good" | "warn" | "danger"; icon: ReactNode }) {
  const tones = {
    neutral: "border-border-soft bg-surface",
    good: "border-moss/30 bg-moss/5",
    warn: "border-brass/30 bg-brass/5",
    danger: "border-rust/40 bg-rust/5",
  };
  const iconTones = { neutral: "text-text-muted", good: "text-moss-soft", warn: "text-brass-soft", danger: "text-rust-soft" };
  return (
    <motion.div layout className={`rounded-[5px] border p-4 ${tones[tone]}`}>
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-[10px] uppercase tracking-[0.16em] text-text-faint">{label}</p>
          <p className="font-display text-2xl font-semibold text-text mt-1">{value}</p>
        </div>
        <div className={iconTones[tone]}>{icon}</div>
      </div>
      <p className="text-[11px] text-text-muted mt-2">{caption}</p>
    </motion.div>
  );
}

function StatusPill({ ok, okLabel, badLabel }: { ok: boolean; okLabel: string; badLabel: string }) {
  const cls = ok ? "border-moss/30 bg-moss/10 text-moss-soft" : "border-rust/30 bg-rust/10 text-rust-soft";
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[10.5px] font-semibold ${cls}`}>
      <span className={`h-1.5 w-1.5 rounded-full ${ok ? "bg-moss" : "bg-rust"}`} />
      {ok ? okLabel : badLabel}
    </span>
  );
}

function CapacityBar({ label, value, max, detail }: { label: string; value: number; max: number; detail: string }) {
  const percent = pct(value, max);
  const tone = percent >= 90 ? "bg-rust" : percent >= 75 ? "bg-brass" : "bg-moss";
  return (
    <div>
      <div className="flex items-center justify-between text-[11px] mb-1.5">
        <span className="text-text-muted">{label}</span>
        <span className="font-mono text-text">{detail}</span>
      </div>
      <div className="h-2 rounded-full bg-ink-soft overflow-hidden border border-border-soft">
        <motion.div initial={{ width: 0 }} animate={{ width: `${percent}%` }} transition={{ duration: 0.7 }} className={`h-full rounded-full ${tone}`} />
      </div>
    </div>
  );
}

export function DatabaseHealthPanel() {
  const { demo } = useAuth();
  const beginRequest = useRequestGuard();
  const [snapshot, setSnapshot] = useState<DbPoolDiagnostics | null>(null);
  const [history, setHistory] = useState<Array<{ time: string; postgres: number; waiting: number; checkedOut: number }>>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);

  const refresh = useCallback(async (silent = false) => {
    const isCurrent = beginRequest();
    if (silent) setRefreshing(true); else setLoading(true);
    setError(null);
    try {
      const data = await diagnosticsApi.dbPool();
      if (!isCurrent()) return;
      setSnapshot(data);
      setLastUpdated(new Date());
      setHistory((prev) => [...prev, {
        time: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }),
        postgres: data.postgres_activity?.total_connections ?? 0,
        waiting: data.pgbouncer_pool?.cl_waiting ?? 0,
        checkedOut: data.sqlalchemy_pool?.checked_out ?? 0,
      }].slice(-18));
    } catch (err) {
      if (!isCurrent()) return;
      if (demo) {
        setSnapshot(DEMO_SNAPSHOT);
        setLastUpdated(new Date());
        setHistory((prev) => [...prev, { time: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }), postgres: 18, waiting: 0, checkedOut: 3 }].slice(-18));
      } else {
        setError(err instanceof ApiError && err.status === 403 ? "Database diagnostics are restricted to the true Super Admin account." : err instanceof Error ? err.message : "Unable to load database diagnostics.");
      }
    } finally {
      if (isCurrent()) { setLoading(false); setRefreshing(false); }
    }
  }, [beginRequest, demo]);

  useEffect(() => { refresh(); }, [refresh]);

  // Each poll opens a short-lived probe connection to PgBouncer's admin
  // console (see backend/db_pool_metrics.py), so polling a backgrounded
  // tab wastes both browser cycles and real database connections for no
  // one to see. Pause the interval while the tab is hidden and catch up
  // with one immediate refresh when it becomes visible again.
  useEffect(() => {
    let timer: number | null = null;
    const start = () => {
      if (timer != null) return;
      timer = window.setInterval(() => refresh(true), 30_000);
    };
    const stop = () => {
      if (timer == null) return;
      window.clearInterval(timer);
      timer = null;
    };
    const onVisibilityChange = () => {
      if (document.hidden) {
        stop();
      } else {
        refresh(true);
        start();
      }
    };
    if (!document.hidden) start();
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [refresh]);

  const health = useMemo(() => healthFor(snapshot), [snapshot]);
  const pg = snapshot?.postgres_activity;
  const pool = snapshot?.sqlalchemy_pool;
  const b = snapshot?.pgbouncer_pool;
  const route = snapshot?.database_route;
  const configured = snapshot?.configured;
  const routeInUse = route?.in_use === true;
  const bouncerReachable = b?.reachable === true;
  const healthClass = health.level === "healthy" ? "border-moss/40 bg-moss/10 text-moss-soft" : health.level === "attention" ? "border-brass/40 bg-brass/10 text-brass-soft" : health.level === "critical" ? "border-rust/40 bg-rust/10 text-rust-soft" : "border-border-soft bg-surface-raised text-text-muted";

  if (loading && !snapshot) {
    return <div className="rounded-[5px] border border-border-soft bg-surface p-10 flex items-center justify-center gap-2 text-text-muted text-[13px]"><Loader2 size={15} className="animate-spin" /> Sampling database health…</div>;
  }

  return (
    <div className="space-y-5">
      <div className="rounded-[6px] border border-border-soft bg-surface overflow-hidden">
        <div className="relative p-5 sm:p-6 bg-gradient-to-br from-surface via-surface to-ink-soft">
          <div className="absolute inset-0 pointer-events-none opacity-20 bg-[radial-gradient(circle_at_80%_20%,var(--color-brass),transparent_38%)]" />
          <div className="relative flex flex-col lg:flex-row lg:items-center justify-between gap-5">
            <div>
              <div className="flex items-center gap-2 text-[10px] uppercase tracking-[0.18em] text-text-faint"><ShieldCheck size={13} className="text-brass" /> Super Admin · System Health</div>
              <h2 className="font-display text-2xl sm:text-3xl font-semibold text-text mt-2">Database command center</h2>
              <p className="text-[12.5px] text-text-muted mt-2 max-w-2xl">A live, read-only view of the API pool, PgBouncer and PostgreSQL connection budget. Use it to spot saturation before users feel it.</p>
            </div>
            <div className="flex items-center gap-2 shrink-0">
              <div className={`flex items-center gap-2 rounded-full border px-3 py-1.5 text-[11px] font-semibold ${healthClass}`}><StatusIcon level={health.level} /> {health.label}</div>
              <button onClick={() => refresh(true)} disabled={refreshing} title="Refresh now" aria-label="Refresh database diagnostics" className="h-9 w-9 flex items-center justify-center rounded-full border border-border-soft bg-surface hover:border-border text-text-muted hover:text-text disabled:opacity-50 transition-colors">
                <RefreshCw size={14} className={refreshing ? "animate-spin" : ""} />
              </button>
            </div>
          </div>
          <div className="relative mt-5 flex flex-wrap items-center gap-x-4 gap-y-1 text-[10.5px] text-text-faint">
            <span className="flex items-center gap-1.5"><span className={`h-1.5 w-1.5 rounded-full ${demo ? "bg-brass" : "bg-moss"}`} /> {demo ? "Demo snapshot" : "Live snapshot"}</span>
            <span>Auto-refresh: 30s</span>
            {lastUpdated && <span>Updated {lastUpdated.toLocaleTimeString()}</span>}
            {configured && <span>PgBouncer {configured.use_pgbouncer ? "enabled" : "disabled"}</span>}
          </div>
        </div>
      </div>

      {error && <ErrorBanner>{error}</ErrorBanner>}

      <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-3">
        <MetricCard label="PostgreSQL connections" value={`${pg?.total_connections ?? "—"} / ${pg?.max_connections ?? "—"}`} caption="Total sessions against the database" tone={pct(pg?.total_connections ?? 0, pg?.max_connections ?? 0) >= 75 ? "warn" : "good"} icon={<Database size={19} />} />
        <MetricCard label="PgBouncer waiting" value={b?.cl_waiting ?? "—"} caption="Clients waiting for a server connection" tone={(b?.cl_waiting ?? 0) > 0 ? "warn" : "good"} icon={<Waves size={19} />} />
        <MetricCard label="API checked out" value={pool?.checked_out ?? "—"} caption={`Of ${pool?.pool_size ?? "—"} SQLAlchemy pool slots`} tone={(pool?.overflow ?? 0) > 0 ? "warn" : "good"} icon={<Activity size={19} />} />
        <MetricCard label="Idle in transaction" value={pg?.idle_in_transaction ?? "—"} caption="Sessions holding an unfinished transaction" tone={(pg?.idle_in_transaction ?? 0) > 0 ? "warn" : "good"} icon={<Timer size={19} />} />
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-5 gap-4">
        <section className="xl:col-span-3 rounded-[5px] border border-border-soft bg-surface p-5">
          <div className="flex items-center justify-between gap-3 mb-4">
            <div><h3 className="font-display text-sm font-semibold text-text">Connection pressure</h3><p className="text-[11px] text-text-muted mt-1">Recent samples from this browser session</p></div>
            <Gauge size={17} className="text-brass" />
          </div>
          <div className="h-56">
            {history.length > 1 ? (
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={history} margin={{ top: 8, right: 8, left: -24, bottom: 0 }}>
                  <defs><linearGradient id="dbPressure" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="var(--color-brass)" stopOpacity={0.35} /><stop offset="100%" stopColor="var(--color-brass)" stopOpacity={0} /></linearGradient></defs>
                  <CartesianGrid stroke="var(--color-border-soft)" strokeDasharray="3 3" vertical={false} />
                  <XAxis dataKey="time" tick={{ fill: "var(--color-text-faint)", fontSize: 9 }} tickLine={false} axisLine={false} />
                  <YAxis allowDecimals={false} tick={{ fill: "var(--color-text-faint)", fontSize: 9 }} tickLine={false} axisLine={false} />
                  <Tooltip contentStyle={{ background: "var(--color-surface-raised)", border: "1px solid var(--color-border)", borderRadius: 4, fontSize: 11 }} labelStyle={{ color: "var(--color-text-muted)" }} />
                  <Area type="monotone" dataKey="postgres" name="Postgres" stroke="var(--color-brass)" fill="url(#dbPressure)" strokeWidth={2} />
                  <Area type="monotone" dataKey="waiting" name="PgBouncer waiting" stroke="var(--color-rust-soft)" fill="transparent" strokeWidth={2} />
                  <Area type="monotone" dataKey="checkedOut" name="API checked out" stroke="var(--color-sky)" fill="transparent" strokeWidth={2} />
                </AreaChart>
              </ResponsiveContainer>
            ) : <div className="h-full flex items-center justify-center text-[12px] text-text-faint">Collecting samples…</div>}
          </div>
        </section>

        <section className="xl:col-span-2 rounded-[5px] border border-border-soft bg-surface p-5">
          <div className="flex items-center gap-2 mb-5"><Server size={17} className="text-brass" /><div><h3 className="font-display text-sm font-semibold text-text">PostgreSQL capacity</h3><p className="text-[11px] text-text-muted mt-1">Ground truth from pg_stat_activity</p></div></div>
          <div className="space-y-5">
            <CapacityBar label="Connection budget" value={pg?.total_connections ?? 0} max={pg?.max_connections ?? 0} detail={`${pg?.total_connections ?? 0} / ${pg?.max_connections ?? 0}`} />
            <CapacityBar label="Active sessions" value={pg?.active ?? 0} max={pg?.max_connections ?? 0} detail={`${pg?.active ?? 0} active`} />
            <div className="grid grid-cols-2 gap-2 pt-1">
              <div className="rounded-[4px] border border-border-soft bg-ink-soft p-3"><p className="text-[10px] uppercase tracking-wider text-text-faint">Idle</p><p className="font-mono text-lg text-text mt-1">{pg?.idle ?? "—"}</p></div>
              <div className={`rounded-[4px] border p-3 ${(pg?.idle_in_transaction ?? 0) > 0 ? "border-brass/30 bg-brass/5" : "border-border-soft bg-ink-soft"}`}><p className="text-[10px] uppercase tracking-wider text-text-faint">Idle in txn</p><p className="font-mono text-lg text-text mt-1">{pg?.idle_in_transaction ?? "—"}</p></div>
            </div>
          </div>
        </section>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-4 gap-4">
        <section className="rounded-[5px] border border-border-soft bg-surface p-5">
          <div className="flex items-center justify-between gap-2 mb-4">
            <div className="flex items-center gap-2"><Network size={16} className="text-sky" /><h3 className="font-display text-sm font-semibold text-text">Application route</h3></div>
            <StatusPill ok={routeInUse} okLabel="PgBouncer in use" badLabel={route?.configured ? "Configured, not in use" : "Direct PostgreSQL"} />
          </div>
          <dl className="space-y-3 text-[11px]">
            <div className="flex justify-between gap-3"><dt className="text-text-faint">Endpoint</dt><dd className="font-mono text-text">{route?.host ?? "—"}:{route?.port ?? "—"}</dd></div>
            <div className="flex justify-between items-center gap-3"><dt className="text-text-faint">PgBouncer reachable</dt><dd><StatusPill ok={bouncerReachable} okLabel="Healthy" badLabel="No live probe" /></dd></div>
            {route?.configured && (
              <div className="flex justify-between gap-3"><dt className="text-text-faint">Expected pooler</dt><dd className="font-mono text-text">{route?.expected_pooler_host ?? "—"}:{route?.expected_pooler_port ?? "—"}</dd></div>
            )}
          </dl>
        </section>
        <section className="rounded-[5px] border border-border-soft bg-surface p-5">
          <div className="flex items-center gap-2 mb-4"><HardDrive size={16} className="text-sky" /><h3 className="font-display text-sm font-semibold text-text">SQLAlchemy pool</h3></div>
          <div className="grid grid-cols-2 gap-x-5 gap-y-4 text-[11px]">
            <div><span className="text-text-faint">Pool size</span><p className="font-mono text-text text-base mt-1">{pool?.pool_size ?? "—"}</p></div>
            <div><span className="text-text-faint">Checked out</span><p className="font-mono text-text text-base mt-1">{pool?.checked_out ?? "—"}</p></div>
            <div><span className="text-text-faint">Checked in</span><p className="font-mono text-text text-base mt-1">{pool?.checked_in ?? "—"}</p></div>
            <div><span className="text-text-faint">Overflow</span><p className={`font-mono text-base mt-1 ${(pool?.overflow ?? 0) > 0 ? "text-brass-soft" : "text-moss-soft"}`}>{pool?.overflow ?? "—"}</p></div>
          </div>
        </section>
        <section className="rounded-[5px] border border-border-soft bg-surface p-5">
          <div className="flex items-center justify-between gap-2 mb-4">
            <div className="flex items-center gap-2"><Waves size={16} className="text-moss-soft" /><h3 className="font-display text-sm font-semibold text-text">PgBouncer</h3></div>
            <StatusPill ok={bouncerReachable} okLabel="Live" badLabel="Unavailable" />
          </div>
          <div className="grid grid-cols-2 gap-x-5 gap-y-4 text-[11px]">
            <div><span className="text-text-faint">Active clients</span><p className="font-mono text-text text-base mt-1">{b?.cl_active ?? "—"}</p></div>
            <div><span className="text-text-faint">Waiting</span><p className={`font-mono text-base mt-1 ${(b?.cl_waiting ?? 0) > 0 ? "text-brass-soft" : "text-moss-soft"}`}>{b?.cl_waiting ?? "—"}</p></div>
            <div><span className="text-text-faint">Server active</span><p className="font-mono text-text text-base mt-1">{b?.sv_active ?? "—"}</p></div>
            <div><span className="text-text-faint">Server idle</span><p className="font-mono text-text text-base mt-1">{b?.sv_idle ?? "—"}</p></div>
            <div><span className="text-text-faint">Avg query</span><p className="font-mono text-text mt-1">{formatDurationUs(b?.avg_query_time_us)}</p></div>
            <div><span className="text-text-faint">Avg client wait</span><p className="font-mono text-text mt-1">{formatDurationUs(b?.avg_wait_time_us)}</p></div>
            <div><span className="text-text-faint">Pool mode</span><p className="font-mono text-text mt-1">{b?.pool_mode ?? "—"}</p></div>
            <div><span className="text-text-faint">Default / reserve pool</span><p className="font-mono text-text mt-1">{b?.default_pool_size ?? "—"} / {b?.reserve_pool_size ?? "—"}</p></div>
          </div>
        </section>
        <section className="rounded-[5px] border border-border-soft bg-surface p-5">
          <div className="flex items-center gap-2 mb-4"><Gauge size={16} className="text-brass" /><h3 className="font-display text-sm font-semibold text-text">Configured guardrails</h3></div>
          <div className="space-y-3 text-[11px]">
            <div className="flex justify-between gap-3"><span className="text-text-muted">PgBouncer server pool</span><span className="font-mono text-text">{configured?.pgbouncer_server_pool_size ?? "—"}</span></div>
            <div className="flex justify-between gap-3"><span className="text-text-muted">Safety margin</span><span className="font-mono text-text">{configured?.pgbouncer_safety_margin_percent ?? "—"}%</span></div>
            <div className="flex justify-between gap-3"><span className="text-text-muted">Background reserve</span><span className="font-mono text-text">{configured?.db_background_connection_reserve ?? "—"}</span></div>
            <div className="flex justify-between gap-3"><span className="text-text-muted">Background concurrency</span><span className="font-mono text-text">{configured?.db_background_concurrency_limit ?? "—"}</span></div>
            <div className="flex justify-between gap-3"><span className="text-text-muted">DB safety margin</span><span className="font-mono text-text">{configured?.db_connection_safety_margin ?? "—"}</span></div>
          </div>
        </section>
      </div>

      <div className={`rounded-[5px] border p-4 flex items-start gap-3 ${healthClass}`}>
        <StatusIcon level={health.level} />
        <div><p className="text-[12px] font-semibold">{health.label}</p><p className="text-[11px] mt-1 opacity-80">{health.detail}</p></div>
      </div>
    </div>
  );
}
