import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { CustodyDrawer } from "../../src/components/CustodyDrawer";
import type { CustodyItem } from "../../src/lib/types";

// =============================================================================
// components/CustodyDrawerHighlight.test.tsx
// -----------------------------------------------------------------------------
// Covers the global header search's "CO-<id>" deep link (see Layout.tsx's
// submitHeaderSearch() and lib/globalSearch.ts): once a checkout code
// resolves to a holder, CustodyDrawer is opened with
// target.highlightCheckoutId set, and should scroll to and briefly
// highlight that exact row -- reusing the same mechanism its own manual
// "scan their receipt barcode" box already uses (see handleScanSubmit()
// in CustodyDrawer.tsx), rather than a second, separate implementation.
// =============================================================================

const twoItems: CustodyItem[] = vi.hoisted(() => [
  { checkout_id: 12, asset_name: "Lexar Professional 256GB SDXC V60", quantity: 2, outstanding: 2, due_date: "2026-08-06", due_soon: false, overdue: true },
  { checkout_id: 13, asset_name: "fx6 card 960Gb", quantity: 3, outstanding: 3, due_date: "2026-08-06", due_soon: false, overdue: true },
]) as CustodyItem[];

vi.mock("../../src/lib/api", async () => {
  const actual = await vi.importActual<typeof import("../../src/lib/api")>("../../src/lib/api");
  return {
    ...actual,
    usersApi: { ...actual.usersApi, items: vi.fn().mockResolvedValue({ assigned_items: twoItems }) },
  };
});

beforeEach(() => {
  vi.clearAllMocks();
  // jsdom doesn't implement scrollIntoView -- stub it so the highlight
  // effect's call to it doesn't throw, same as any other DOM API jsdom
  // is missing.
  Element.prototype.scrollIntoView = vi.fn();
});

describe("CustodyDrawer highlightCheckoutId", () => {
  it("scrolls to and highlights the exact row named by the checkout code, not just whichever items happen to load first", async () => {
    render(
      <CustodyDrawer
        target={{ type: "user", id: 7, name: "D. Martins", highlightCheckoutId: 13 }}
        onClose={() => {}}
      />
    );

    await screen.findByText("fx6 card 960Gb");
    const targetRow = screen.getByText("fx6 card 960Gb").closest("div.border");
    expect(targetRow).not.toBeNull();

    await waitFor(() => expect(targetRow).toHaveClass("border-brass"));
    expect(Element.prototype.scrollIntoView).toHaveBeenCalled();

    // The other row on the same ledger was never the target -- it should
    // never pick up the highlight styling.
    const otherRow = screen.getByText("Lexar Professional 256GB SDXC V60").closest("div.border");
    expect(otherRow).not.toHaveClass("border-brass");
  });

  it("doesn't highlight anything when no highlightCheckoutId is given (ordinary open)", async () => {
    render(
      <CustodyDrawer
        target={{ type: "user", id: 7, name: "D. Martins" }}
        onClose={() => {}}
      />
    );

    await screen.findByText("fx6 card 960Gb");
    expect(Element.prototype.scrollIntoView).not.toHaveBeenCalled();
    expect(screen.getByText("fx6 card 960Gb").closest("div.border")).not.toHaveClass("border-brass");
  });
});
