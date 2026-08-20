import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
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
    dbPoolMock.mockResolvedValue(SNAPSHOT);
    render(<DatabaseHealthPanel />);

    await screen.findByText("Application route");
    expect(screen.getAllByText("Attention").length).toBeGreaterThan(0);
    expect(screen.getByText(/isn't actually routed through it right now/i)).toBeInTheDocument();
  });
});
