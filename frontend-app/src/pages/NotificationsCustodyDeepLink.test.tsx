import { describe, it, expect, vi, beforeEach } from "vitest";
import { StrictMode } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { Notifications } from "./Notifications";
import { Admin } from "./Admin";
import type { AuthUser, Checkout } from "../lib/api";

// =============================================================================
// pages/NotificationsCustodyDeepLink.test.tsx
// -----------------------------------------------------------------------------
// Covers the full click-through described in Notifications.tsx's own
// "CLICK-THROUGH" comment: tapping "View ->" on a grouped Overdue/Due
// Soon/Extension Requests row should navigate straight into that person's
// Custody Ledger on the Admin page -- the correct directory tab (User vs
// Ad-Hoc), with the drawer already open, not just the bare directory list.
//
// Rendered under <StrictMode>, same as src/main.tsx wraps the real app in,
// since React's dev-mode double-invocation of effects on mount is exactly
// the kind of thing that can silently break a deep-link effect keyed off
// an unmemoized object identity (see Admin.tsx's deepLinkTarget).
// =============================================================================

const { useAuthMock, outsiderDueSoonCheckout, userOverdueCheckout } = vi.hoisted(() => ({
  useAuthMock: vi.fn(),
  // Matches backend/services/checkout_service.py's _resolve_assignee(): an
  // Outsider's label is always "<name> (<company or 'No Company'>)".
  outsiderDueSoonCheckout: {
    checkout_id: 1,
    asset_name: "Dell UltraSharp U2723QE Monitor",
    checked_out_to: "Samuel Jude Godson (No Company)",
    entity_id: 42,
    entity_type: "outsider",
    due_date: "2026-08-15",
    overdue: false,
  } as unknown as Checkout,
  userOverdueCheckout: {
    checkout_id: 2,
    asset_name: "MacBook Pro 14\" M3",
    checked_out_to: "T. Okafor",
    entity_id: 7,
    entity_type: "user",
    due_date: "2026-08-01",
    overdue: true,
  } as unknown as Checkout,
}));

vi.mock("../lib/useAuth", () => ({ useAuth: useAuthMock }));

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return {
    ...actual,
    myItemsApi: { ...actual.myItemsApi, list: vi.fn().mockResolvedValue({ assigned_items: [] }) },
    extensionsApi: { ...actual.extensionsApi, myDecisions: vi.fn().mockResolvedValue([]), listPending: vi.fn().mockResolvedValue([]) },
    alertsApi: {
      ...actual.alertsApi,
      overdue: vi.fn().mockResolvedValue({ items: [userOverdueCheckout], total: 1 }),
      dueSoon: vi.fn().mockResolvedValue({ items: [outsiderDueSoonCheckout], total: 1 }),
    },
    usersApi: {
      ...actual.usersApi,
      list: vi.fn().mockResolvedValue({ items: [], total: 0, limit: 20, offset: 0 }),
      items: vi.fn().mockResolvedValue({ assigned_items: [] }),
    },
    outsidersApi: {
      ...actual.outsidersApi,
      list: vi.fn().mockResolvedValue({ items: [], total: 0, limit: 20, offset: 0 }),
      items: vi.fn().mockResolvedValue({ assigned_items: [] }),
    },
    quotationsApi: { ...actual.quotationsApi, list: vi.fn().mockResolvedValue({ items: [], total: 0, limit: 20, offset: 0 }), catalog: vi.fn().mockResolvedValue([]), fulfillmentQueue: vi.fn().mockResolvedValue([]) },
    auditApi: { ...actual.auditApi, list: vi.fn().mockResolvedValue({ items: [], total: 0, limit: 20, offset: 0 }) },
  };
});

function renderFromNotifications() {
  useAuthMock.mockReturnValue({ user: { name: "Admin", email: "a@a.com", role: "admin" } satisfies AuthUser, demo: false });
  return render(
    <StrictMode>
      <MemoryRouter initialEntries={["/notifications"]}>
        <Routes>
          <Route path="/notifications" element={<Notifications />} />
          <Route path="/admin" element={<Admin />} />
        </Routes>
      </MemoryRouter>
    </StrictMode>
  );
}

beforeEach(() => vi.clearAllMocks());

describe("Notifications 'View ->' click-through opens the right Custody Ledger", () => {
  it("an Ad-Hoc Outsider's Due Soon row opens the Ad-Hoc Directory's drawer, not the default User Directory", async () => {
    const user = userEvent.setup();
    renderFromNotifications();

    const row = (await screen.findByText(/Samuel Jude Godson/)).closest("li");
    expect(row).not.toBeNull();
    await user.click(row!);

    await screen.findByRole("heading", { name: "Admin" });
    await waitFor(() => expect(screen.getByText("Ad-Hoc Directory")).toHaveClass("bg-brass/15"));
    expect(screen.getByText("User Directory")).not.toHaveClass("bg-brass/15");
    await waitFor(() => expect(screen.getByText("Custody ledger")).toBeInTheDocument());
    expect(screen.getByRole("heading", { name: "Samuel Jude Godson (No Company)" })).toBeInTheDocument();
  });

  it("a linked User's Overdue row opens the User Directory's drawer", async () => {
    const user = userEvent.setup();
    renderFromNotifications();

    const row = (await screen.findByText(/T\. Okafor/)).closest("li");
    expect(row).not.toBeNull();
    await user.click(row!);

    await screen.findByRole("heading", { name: "Admin" });
    await waitFor(() => expect(screen.getByText("User Directory")).toHaveClass("bg-brass/15"));
    await waitFor(() => expect(screen.getByText("Custody ledger")).toBeInTheDocument());
    expect(screen.getByRole("heading", { name: "T. Okafor" })).toBeInTheDocument();
  });
});
