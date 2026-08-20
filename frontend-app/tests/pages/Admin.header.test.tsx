// <Admin /> and <Manager /> (src/pages/Admin.tsx) render from the SAME
// underlying panel set/component tree, differing only in which page
// component wraps it -- these tests guard against that shared plumbing
// leaking the wrong header/pill/tab-set between the two roles (e.g. a
// `manager`-role user hitting the /manager route must never see
// Admin-only tabs, and a full `admin`/`super_admin` visiting /manager
// directly must still get the restricted Manager view, not an
// role-upgraded one).
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { Admin, Manager } from "../../src/pages/Admin";
import { CustodyProvider } from "../../src/lib/custodyContext";
import type { AuthUser } from "../../src/lib/api";

// vi.hoisted so this mock factory (below) can reference useAuthMock even
// though vi.mock() calls are hoisted above the rest of the module.
const { useAuthMock } = vi.hoisted(() => ({ useAuthMock: vi.fn() }));

vi.mock("../../src/lib/useAuth", () => ({ useAuth: useAuthMock }));

// Stub out every list/catalog network call the Admin/Manager panels make
// on mount with empty-but-valid responses -- this suite only cares about
// header/tab-set rendering, not the panels' own data, and importActual
// keeps every other export (types, non-mocked API functions) real.
vi.mock("../../src/lib/api", async () => {
  const actual = await vi.importActual<typeof import("../../src/lib/api")>("../../src/lib/api");
  return {
    ...actual,
    usersApi: { ...actual.usersApi, list: vi.fn().mockResolvedValue({ items: [], total: 0, limit: 20, offset: 0 }) },
    outsidersApi: { ...actual.outsidersApi, list: vi.fn().mockResolvedValue({ items: [], total: 0, limit: 20, offset: 0 }) },
    quotationsApi: { ...actual.quotationsApi, list: vi.fn().mockResolvedValue({ items: [], total: 0, limit: 20, offset: 0 }), catalog: vi.fn().mockResolvedValue([]), fulfillmentQueue: vi.fn().mockResolvedValue([]) },
    auditApi: { ...actual.auditApi, list: vi.fn().mockResolvedValue({ items: [], total: 0, limit: 20, offset: 0 }) },
  };
});

// Renders either page as a given logged-in role -- CustodyProvider is
// required because the shared panel tree reads custody-drawer context
// even when nothing in these tests actually opens the drawer.
function renderPage(Page: typeof Admin | typeof Manager, role: string) {
  useAuthMock.mockReturnValue({
    user: { name: "Test User", email: "t@example.com", role } satisfies AuthUser,
    demo: false,
  });
  return render(
    <MemoryRouter>
      <CustodyProvider>
        <Page />
      </CustodyProvider>
    </MemoryRouter>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("<Admin /> / <Manager /> -- separate pages", () => {
  it("<Manager /> always shows the Manager header and pill", async () => {
    renderPage(Manager, "manager");

    expect(await screen.findByRole("heading", { name: "Manager" })).toBeInTheDocument();
    expect(screen.getByText("Manager Mode")).toBeInTheDocument();
    expect(screen.queryByText("Admin Mode")).not.toBeInTheDocument();
  });

  it("<Admin /> always shows the Admin header and pill for a plain admin account", async () => {
    renderPage(Admin, "admin");

    expect(await screen.findByRole("heading", { name: "Admin" })).toBeInTheDocument();
    expect(screen.getByText("Admin Mode")).toBeInTheDocument();
    expect(screen.queryByText("Manager Mode")).not.toBeInTheDocument();
  });

  it("<Admin /> always shows the Admin header and pill for a super_admin account", async () => {
    renderPage(Admin, "super_admin");

    expect(await screen.findByRole("heading", { name: "Admin" })).toBeInTheDocument();
    expect(screen.getByText("Admin Mode")).toBeInTheDocument();
  });

  it("<Manager /> never offers the Admin-only Import/Backups tabs", async () => {
    renderPage(Manager, "manager");

    await screen.findByRole("heading", { name: "Manager" });
    expect(screen.queryByText("Inventory Import")).not.toBeInTheDocument();
    expect(screen.queryByText("System Backups")).not.toBeInTheDocument();
    expect(screen.getByText("User Directory")).toBeInTheDocument();
    expect(screen.getByText("Quotes")).toBeInTheDocument();
  });

  it("<Manager /> still omits Import/Backups even when a full admin views it directly", async () => {
    renderPage(Manager, "admin");

    await screen.findByRole("heading", { name: "Manager" });
    expect(screen.queryByText("Inventory Import")).not.toBeInTheDocument();
    expect(screen.queryByText("System Backups")).not.toBeInTheDocument();
  });
});
