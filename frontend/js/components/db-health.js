import { apiRequest } from '../api.js';

const esc = (value) => String(value ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const num = (v) => Number.isFinite(Number(v)) ? Number(v).toLocaleString() : '—';

function statusPill(ok, label) {
  const cls = ok ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-400' : 'border-rose-500/30 bg-rose-500/10 text-rose-400';
  return `<span class="inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-semibold ${cls}"><span class="h-1.5 w-1.5 rounded-full ${ok ? 'bg-emerald-400' : 'bg-rose-400'}"></span>${esc(label)}</span>`;
}

export async function loadDbHealth() {
  const root = document.getElementById('dbHealthContent');
  if (!root) return;
  root.innerHTML = '<div class="py-8 text-center text-[13px] text-slate-500">Checking live database routing and pool capacity…</div>';
  try {
    const data = await apiRequest('/diagnostics/db-pool');
    const route = data.database_route || {};
    const pg = data.pgbouncer_pool;
    const sql = data.sqlalchemy_pool || {};
    const pa = data.postgres_activity || {};
    const cfg = data.configured || {};
    const inUse = route.in_use === true;
    const reachable = pg?.reachable === true;
    const waiting = Number(pg?.cl_waiting || 0);
    const serverTotal = Number(pg?.sv_active || 0) + Number(pg?.sv_idle || 0);
    const serverCapacity = pg?.default_pool_size != null ? Number(pg.default_pool_size) + Number(pg.reserve_pool_size || 0) : cfg.pgbouncer_server_pool_size;
    const pressure = serverCapacity ? Math.round((serverTotal / serverCapacity) * 100) : null;
    root.innerHTML = `
      <div class="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div class="rounded-xl border border-border bg-card p-4">
          <div class="flex items-center justify-between"><h3 class="text-[13px] font-semibold text-slate-100">Application route</h3>${statusPill(inUse, inUse ? 'PgBouncer in use' : (cfg.use_pgbouncer ? 'Configured, not in use' : 'Direct PostgreSQL'))}</div>
          <dl class="mt-4 space-y-2 text-[12px]"><div class="flex justify-between gap-3"><dt class="text-slate-500">Endpoint</dt><dd class="font-mono text-slate-300">${esc(route.host || '—')}:${esc(route.port || '—')}</dd></div><div class="flex justify-between gap-3"><dt class="text-slate-500">PgBouncer reachable</dt><dd>${statusPill(reachable, reachable ? 'Healthy' : 'No live admin probe')}</dd></div></dl>
        </div>
        <div class="rounded-xl border border-border bg-card p-4">
          <div class="flex items-center justify-between"><h3 class="text-[13px] font-semibold text-slate-100">PgBouncer pool</h3>${statusPill(reachable, reachable ? 'Live' : 'Unavailable')}</div>
          <dl class="mt-4 grid grid-cols-2 gap-3 text-[12px]"><div><dt class="text-slate-500">Clients</dt><dd class="mt-1 text-[16px] font-bold text-slate-100">${num(pg?.cl_active)}</dd></div><div><dt class="text-slate-500">Waiting</dt><dd class="mt-1 text-[16px] font-bold ${waiting ? 'text-amber-400' : 'text-emerald-400'}">${num(waiting)}</dd></div><div><dt class="text-slate-500">Server active</dt><dd class="mt-1 font-semibold text-slate-200">${num(pg?.sv_active)}</dd></div><div><dt class="text-slate-500">Server idle</dt><dd class="mt-1 font-semibold text-slate-200">${num(pg?.sv_idle)}</dd></div></dl>
        </div>
        <div class="rounded-xl border border-border bg-card p-4">
          <div class="flex items-center justify-between"><h3 class="text-[13px] font-semibold text-slate-100">PostgreSQL</h3>${statusPill(pa.total_connections != null, pa.total_connections != null ? 'Connected' : 'Probe unavailable')}</div>
          <dl class="mt-4 grid grid-cols-2 gap-3 text-[12px]"><div><dt class="text-slate-500">Connections</dt><dd class="mt-1 text-[16px] font-bold text-slate-100">${num(pa.total_connections)} / ${num(pa.max_connections)}</dd></div><div><dt class="text-slate-500">Active</dt><dd class="mt-1 font-semibold text-slate-200">${num(pa.active)}</dd></div><div><dt class="text-slate-500">Idle</dt><dd class="mt-1 font-semibold text-slate-200">${num(pa.idle)}</dd></div><div><dt class="text-slate-500">Idle in txn</dt><dd class="mt-1 font-semibold ${pa.idle_in_transaction ? 'text-amber-400' : 'text-slate-200'}">${num(pa.idle_in_transaction)}</dd></div></dl>
        </div>
      </div>
      <div class="mt-4 rounded-xl border border-border bg-card p-4">
        <div class="flex flex-wrap items-center justify-between gap-3"><div><h3 class="text-[13px] font-semibold text-slate-100">Connection budgets</h3><p class="mt-1 text-[12px] text-slate-500">These are separate layers. Client connections can be much larger than real PostgreSQL server connections.</p></div><button data-action="refresh-db-health" class="rounded-md border border-border bg-card2 px-3 py-1.5 text-[12px] font-medium text-slate-300 hover:border-blue-500/50 hover:text-blue-400">Refresh</button></div>
        <div class="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <div class="rounded-lg bg-card2/60 p-3"><p class="text-[11px] text-slate-500">SQLAlchemy checked out</p><p class="mt-1 text-lg font-bold text-slate-100">${num(sql.checked_out)}</p></div>
          <div class="rounded-lg bg-card2/60 p-3"><p class="text-[11px] text-slate-500">SQLAlchemy pool size</p><p class="mt-1 text-lg font-bold text-slate-100">${num(sql.pool_size)}</p></div>
          <div class="rounded-lg bg-card2/60 p-3"><p class="text-[11px] text-slate-500">PgBouncer server use</p><p class="mt-1 text-lg font-bold text-slate-100">${num(serverTotal)} / ${num(serverCapacity)}</p></div>
          <div class="rounded-lg bg-card2/60 p-3"><p class="text-[11px] text-slate-500">Pool pressure</p><p class="mt-1 text-lg font-bold ${pressure != null && pressure >= 90 ? 'text-amber-400' : 'text-slate-100'}">${pressure != null ? pressure + '%' : '—'}</p></div>
        </div>
        <div class="mt-4 grid grid-cols-1 gap-2 text-[12px] text-slate-400 sm:grid-cols-3"><div>Pool mode: <span class="font-mono text-slate-200">${esc(pg?.pool_mode || '—')}</span></div><div>Default pool: <span class="font-mono text-slate-200">${num(pg?.default_pool_size)}</span></div><div>Reserve pool: <span class="font-mono text-slate-200">${num(pg?.reserve_pool_size)}</span></div></div>
      </div>`;
  } catch (err) {
    root.innerHTML = `<div class="rounded-xl border border-rose-500/30 bg-rose-500/5 p-4 text-[13px] text-rose-300">Unable to load database diagnostics. ${esc(err?.message || 'Please retry.')}</div>`;
  }
}
