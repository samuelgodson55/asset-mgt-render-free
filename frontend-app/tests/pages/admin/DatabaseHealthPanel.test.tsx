// Covers src/pages/admin/DatabaseHealthPanel.tsx (the React SPA's
// equivalent of frontend/js/components/db-health.js) against a fixed,
// hand-built GET /diagnostics/db-pool snapshot. The snapshot deliberately
// describes a "configured but not actually in use" PgBouncer -- i.e.
// USE_PGBOUNCER=true but database_route.in_use is false -- specifically to
// exercise the panel's mismatch-detection/"Attention" warning path, not
// just its happy-path rendering.
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { DatabaseHealthPanel } from "../../../src/pages/admin/DatabaseHealthPanel";
import type { DbPoolDiagnostics } from "../../../src/lib/types";

const { useAuthMock, dbPoolMock } = vi.hoisted(() => ({
  useAuthMock: vi.fn(),
  dbPoolMock: vi.fn(),
}));

vi.mock("../../../src/lib/useAuth", () => ({ useAuth: useAuthMock }));

vi.mock("../../../src/lib/api", async () => {
  const actual = await vi.importActual<typeof import("../../../src/lib/api")>("../../../src/lib/api");
  return {
    ...actual,
    diagnosticsApi: { dbPool: dbPoolMock },
  };
});

// A realistic full snapshot of GET /diagnostics/db-pool's response shape
// (see backend/db_pool_metrics.py) -- reused across every test below so
// each one only needs to assert on the rendering, not rebuild the fixture.
const SNAPSHOT: DbPoolDiagnostics = {
  database_route: {
    configured: true,
    in_use: false,
    host: "db-direct.internal",
    port: 5432,
    expected_pooler_host: "pgbouncer.internal",
    expected_pooler_port: 6432,
  },
  sqlalchemy_pool: { pool_size: 10, checked_out: 2, checked_in: 8, overflow: 0 },
  pgbouncer_pool: {
    reachable: false,
    in_use: false,
    cl_active: 0,
    cl_waiting: 0,
    sv_active: 0,
    sv_idle: 0,
    sv_used: 0,
    maxwait_seconds: 0,
    avg_query_time_us: 0,
    avg_wait_time_us: 0,
    pool_mode: "transaction",
    max_client_conn: 200,
    default_pool_size: 12,
    reserve_pool_size: 4,
  },
  postgres_activity: { max_connections: 100, total_connections: 12, active: 3, idle: 9, idle_in_transaction: 0 },
  configured: {
    use_pgbouncer: true,
    pgbouncer_server_pool_size: 12,
    pgbouncer_safety_margin_percent: 20,
    db_background_connection_reserve: 2,
    db_background_concurrency_limit: 2,
    db_connection_safety_margin: 10,
  },
};

beforeEach(() => {
  vi.clearAllMocks();
  useAuthMock.mockReturnValue({ user: { name: "Super", email: "s@example.com", role: "super_admin" }, demo: false });
});

describe("<DatabaseHealthPanel />", () => {
  it("renders the application route card with live endpoint and pooler mismatch status", async () => {
    dbPoolMock.mockResolvedValue(SNAPSHOT);
    render(<DatabaseHealthPanel />);

    expect(await screen.findByText("Application route")).toBeInTheDocument();
    expect(screen.getByText("db-direct.internal:5432")).toBeInTheDocument();
    expect(screen.getByText("Configured, not in use")).toBeInTheDocument();
    expect(screen.getByText("pgbouncer.internal:6432")).toBeInTheDocument();
  });

  it("surfaces PgBouncer pool_mode and default/reserve pool sizing", async () => {
    dbPoolMock.mockResolvedValue(SNAPSHOT);
    render(<DatabaseHealthPanel />);

    await screen.findByText("Application route");
    expect(screen.getByText("transaction")).toBeInTheDocument();
    expect(screen.getByText("12 / 4")).toBeInTheDocument();
  });

  it("flags attention-level health when PgBouncer is configured but not actually routed", async () => {
    // This is the mismatch case the SNAPSHOT fixture was built for:
    // `configured.use_pgbouncer` is true but `database_route.in_use` is
    // false, which should surface as an explicit warning rather than
    // silently rendering as if everything were fine.
    dbPoolMock.mockResolvedValue(SNAPSHOT);
    render(<DatabaseHealthPanel />);

    await screen.findByText("Application route");
    expect(screen.getAllByText("Attention").length).toBeGreaterThan(0);
    expect(screen.getByText(/isn't actually routed through it right now/i)).toBeInTheDocument();
  });

  it("flags attention-level health and hides the breakdown when the DB role can't see other sessions' state", async () => {
    // Postgres nulls out `state` in pg_stat_activity for every session but
    // the probe's own unless the role is a superuser or pg_read_all_stats
    // member -- the default on Render/Azure Database for
    // PostgreSQL/RDS/Cloud SQL. `limited_visibility: true` must surface
    // as an explicit caveat, not a silent "0 idle in transaction".
    const limitedSnapshot: DbPoolDiagnostics = {
      ...SNAPSHOT,
      database_route: { ...SNAPSHOT.database_route, in_use: true },
      pgbouncer_pool: { ...SNAPSHOT.pgbouncer_pool, reachable: true },
      postgres_activity: { max_connections: 100, total_connections: 12, active: 1, idle: 2, idle_in_transaction: 0, limited_visibility: true },
    };
    dbPoolMock.mockResolvedValue(limitedSnapshot);
    render(<DatabaseHealthPanel />);

    await screen.findByText("Application route");
    expect(screen.getAllByText("Attention").length).toBeGreaterThan(0);
    expect(screen.getByText(/can't see other sessions' state in pg_stat_activity/i)).toBeInTheDocument();
    expect(screen.getByText("Limited visibility")).toBeInTheDocument();
  });

  it("marks the health badge as stale (not silently healthy) when a refresh fails after an earlier successful sample", async () => {
    // First load succeeds with a clean, healthy snapshot; the next poll
    // (triggered here via the manual refresh button) fails. The badge
    // must not keep showing the earlier "Healthy" verdict as if nothing
    // had changed -- see DatabaseHealthPanel.tsx's `refreshFailed` state.
    const healthySnapshot: DbPoolDiagnostics = {
      ...SNAPSHOT,
      database_route: { ...SNAPSHOT.database_route, in_use: true },
      pgbouncer_pool: { ...SNAPSHOT.pgbouncer_pool, reachable: true },
      postgres_activity: { max_connections: 100, total_connections: 12, active: 3, idle: 9, idle_in_transaction: 0, limited_visibility: false },
    };
    dbPoolMock.mockResolvedValueOnce(healthySnapshot);
    render(<DatabaseHealthPanel />);

    await waitFor(() => expect(screen.getAllByText("Healthy").length).toBeGreaterThan(0));
    expect(screen.getByText("Live snapshot")).toBeInTheDocument();

    dbPoolMock.mockRejectedValueOnce(new Error("network error"));
    fireEvent.click(screen.getByLabelText("Refresh database diagnostics"));

    await waitFor(() => expect(screen.getAllByText("Stale").length).toBeGreaterThan(0));
    expect(screen.getByText("Stale snapshot")).toBeInTheDocument();
    expect(screen.getByText(/latest refresh failed/i)).toBeInTheDocument();
    // Old, no-longer-trustworthy verdict must be gone, not just supplemented.
    expect(screen.queryAllByText("Healthy").length).toBe(0);
  });
});
