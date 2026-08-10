import { describe, it, expect, vi, beforeEach } from "vitest";
import { StrictMode } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { Notifications } from "./Notifications";
import { CustodyProvider, useCustody } from "../lib/custodyContext";
import { CustodyDrawer } from "../components/CustodyDrawer";
import type { AuthUser, Checkout } from "../lib/api";

// =============================================================================
// pages/NotificationsCustodyDeepLink.test.tsx
// -----------------------------------------------------------------------------
// Covers the click-through described in Notifications.tsx's own
// "CLICK-THROUGH" comment: tapping "View ->" on a grouped Overdue/Due
// Soon/Extension Requests row should open that person's Custody Ledger
// drawer directly (the correct target -- User vs Ad-Hoc, by id), via the
// shared CustodyProvider (lib/custodyContext.tsx) -- with NO navigation
// away from Notifications required, matching legacy
// components/custody.js's openCustodyModal(): one shared modal, opened by
// id+type from wherever the click happened, never routed through a
// page/tab.
//
// This used to route through /admin?custody=type:id&name=... and rely on
// Admin.tsx forcing the right directory tab open, then a deep-link effect
// inside whichever panel matched -- a chain that landed on the right tab
// but could silently fail to actually open the drawer. Rendered under
// <StrictMode>, same as src/main.tsx wraps the real app in.
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
      items: vi.fn().mockResolvedValue({ assigned_items: [] }),
    },
    outsidersApi: {
      ...actual.outsidersApi,
      items: vi.fn().mockResolvedValue({ assigned_items: [] }),
    },
  };
});

// Same shape as Layout.tsx: the drawer lives above the routed page,
// driven by the shared CustodyProvider, so the test tree mirrors exactly
// how the real app wires this up.
function Harness() {
  const { target, closeCustody } = useCustody();
  return (
    <>
      <Notifications />
      <CustodyDrawer target={target} onClose={closeCustody} />
    </>
  );
}

function renderFromNotifications() {
  useAuthMock.mockReturnValue({ user: { name: "Admin", email: "a@a.com", role: "admin" } satisfies AuthUser, demo: false });
  return render(
    <StrictMode>
      <MemoryRouter initialEntries={["/notifications"]}>
        <CustodyProvider>
          <Routes>
            <Route path="/notifications" element={<Harness />} />
          </Routes>
        </CustodyProvider>
      </MemoryRouter>
    </StrictMode>
  );
}

beforeEach(() => vi.clearAllMocks());

describe("Notifications 'View ->' click-through opens the right Custody Ledger", () => {
  it("an Ad-Hoc Outsider's Due Soon row opens the drawer for that outsider, with no navigation away from Notifications", async () => {
    const user = userEvent.setup();
    renderFromNotifications();

    const row = (await screen.findByText(/Samuel Jude Godson/)).closest("li");
    expect(row).not.toBeNull();
    await user.click(row!);

    // Still on Notifications -- no page/tab navigation involved.
    await screen.findByRole("heading", { name: "Notifications" });
    await waitFor(() => expect(screen.getByText("Custody ledger")).toBeInTheDocument());
    expect(screen.getByRole("heading", { name: "Samuel Jude Godson (No Company)" })).toBeInTheDocument();
  });

  it("a linked User's Overdue row opens the drawer for that user, with no navigation away from Notifications", async () => {
    const user = userEvent.setup();
    renderFromNotifications();

    const row = (await screen.findByText(/T\. Okafor/)).closest("li");
    expect(row).not.toBeNull();
    await user.click(row!);

    await screen.findByRole("heading", { name: "Notifications" });
    await waitFor(() => expect(screen.getByText("Custody ledger")).toBeInTheDocument());
    expect(screen.getByRole("heading", { name: "T. Okafor" })).toBeInTheDocument();
  });
});
