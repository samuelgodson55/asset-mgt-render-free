import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import userEvent from "@testing-library/user-event";
import { Quotations } from "../../src/pages/Quotations";
import type { CatalogAsset, QuotationCartOrDetail, QuotationListRow } from "../../src/lib/types";
import type { AuthUser } from "../../src/lib/api";

const { quotationsApi, usersApi, useAuthMock } = vi.hoisted(() => ({
  quotationsApi: {
    publicConfig: vi.fn(),
    catalog: vi.fn(),
    catalogPage: vi.fn(),
    myCart: vi.fn(),
    myHistory: vi.fn(),
    addToCart: vi.fn(),
    updateCartItem: vi.fn(),
    removeCartItem: vi.fn(),
    submitCart: vi.fn(),
    myQuoteDetail: vi.fn(),
    updateMyQuoteItem: vi.fn(),
    removeMyQuoteItem: vi.fn(),
    addMyQuoteItem: vi.fn(),
    assign: vi.fn(),
  },
  usersApi: {
    list: vi.fn(),
  },
  useAuthMock: vi.fn(),
}));

vi.mock("../../src/lib/api", async () => {
  const actual = await vi.importActual<typeof import("../../src/lib/api")>("../../src/lib/api");
  return { ...actual, quotationsApi, usersApi };
});

vi.mock("../../src/lib/useAuth", () => ({ useAuth: useAuthMock }));

const CATALOG: CatalogAsset[] = [
  { id: 1, name: "Motorola APX 8000", category: "Field Radios", department: "Audio", price: 149, available_quantity: 5, status: "In Stock" },
  { id: 2, name: "Vortex Diamondback HD", category: "Optics", department: "Camera", price: 899, available_quantity: 0, status: "Out of Stock" },
];

const EMPTY_CART: QuotationCartOrDetail = { items: [], subtotal: 0, vat_percent: 7.5, vat_amount: 0, total: 0 };

function cartWithOneItem(): QuotationCartOrDetail {
  return {
    items: [
      {
        item_id: 501,
        asset_name: "Motorola APX 8000",
        category: "Field Radios",
        quantity: 1,
        start_date: "2026-08-10",
        due_date: "2026-08-11",
        days: 1,
        line_total: 149,
      },
    ],
    subtotal: 149,
    vat_percent: 7.5,
    vat_amount: 11.175,
    total: 160.175,
  };
}

const HISTORY: QuotationListRow[] = [
  { id: 9, reference_number: "QT-000009", status: "submitted", submitted_at: "2026-08-01T10:00:00Z", item_count: 2, total: 596 },
];

// Quotations.tsx now reads/writes a `?quotation=` deep-link param (see
// its own useSearchParams effect), so every render needs a Router
// ancestor -- same requirement react-router's own useSearchParams has
// always had, just not previously exercised by this component.
function renderQuotations() {
  return render(
    <MemoryRouter initialEntries={["/quotations"]}>
      <Quotations />
    </MemoryRouter>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  useAuthMock.mockReturnValue({
    user: { name: "Test User", email: "t@example.com", role: "customer" } satisfies AuthUser,
    demo: false,
  });
  quotationsApi.publicConfig.mockResolvedValue({ currency_code: "NGN", site_name: "Ledger" });
  quotationsApi.catalog.mockResolvedValue(CATALOG);
  quotationsApi.catalogPage.mockResolvedValue({ items: CATALOG, total: CATALOG.length, limit: 5, offset: 0 });
  quotationsApi.myCart.mockResolvedValue(EMPTY_CART);
  quotationsApi.myHistory.mockResolvedValue(HISTORY);
});

describe("<Quotations />", () => {
  it("loads and renders the catalog with an empty order", async () => {
    renderQuotations();

    expect(await screen.findByText("Motorola APX 8000")).toBeInTheDocument();
    expect(screen.getByText("Vortex Diamondback HD")).toBeInTheDocument();
    expect(screen.getByText(/your saved order is empty/i)).toBeInTheDocument();
  });

  it("adds a catalog item to the order and shows it in My Order", async () => {
    const user = userEvent.setup();
    quotationsApi.addToCart.mockResolvedValue(cartWithOneItem());
    renderQuotations();

    const row = (await screen.findByText("Motorola APX 8000")).closest('[data-testid="catalog-row"]') as HTMLElement;
    await user.click(within(row).getByRole("button", { name: /add/i }));

    await waitFor(() => expect(quotationsApi.addToCart).toHaveBeenCalledWith(1, 1, expect.any(String), expect.any(String)));
    expect((await screen.findAllByText(/149\.00/)).length).toBeGreaterThan(0);
    expect(screen.queryByText(/your saved order is empty/i)).not.toBeInTheDocument();
  });

  it("blocks adding to the order when the due date is before the start date", async () => {
    const user = userEvent.setup();
    renderQuotations();

    const row = (await screen.findByText("Motorola APX 8000")).closest('[data-testid="catalog-row"]') as HTMLElement;
    const dateInputs = within(row).getAllByDisplayValue(/\d{4}-\d{2}-\d{2}/) as HTMLInputElement[];
    const [startInput, dueInput] = dateInputs;
    const earlier = new Date(Date.now() - 86400000).toISOString().slice(0, 10);

    await user.clear(dueInput);
    await user.type(dueInput, earlier);
    void startInput;
    await user.click(within(row).getByRole("button", { name: /add/i }));

    expect(await screen.findByText(/due date cannot be before the start date/i)).toBeInTheDocument();
    expect(quotationsApi.addToCart).not.toHaveBeenCalled();
  });

  it("switches to My Quotes and shows submitted quotation history", async () => {
    const user = userEvent.setup();
    renderQuotations();

    await screen.findByText("Motorola APX 8000");
    await user.click(screen.getByRole("button", { name: /my quotes/i }));

    expect(await screen.findByText("QT-000009")).toBeInTheDocument();
    expect(screen.getByText(/pending/i)).toBeInTheDocument();
  });

  it("opens a quote's detail drawer when a history row is clicked", async () => {
    const user = userEvent.setup();
    quotationsApi.myQuoteDetail.mockResolvedValue({
      id: 9,
      reference_number: "QT-000009",
      status: "submitted",
      items: [],
      subtotal: 0,
      vat_percent: 7.5,
      vat_amount: 0,
      total: 0,
    });
    renderQuotations();

    await screen.findByText("Motorola APX 8000");
    await user.click(screen.getByRole("button", { name: /my quotes/i }));
    await user.click(await screen.findByTestId("history-row"));

    await waitFor(() => expect(quotationsApi.myQuoteDetail).toHaveBeenCalledWith(9));
    expect(await screen.findByText("No items on this quote.")).toBeInTheDocument();
  });

  it("submits the order and switches to the history tab", async () => {
    const user = userEvent.setup();
    quotationsApi.myCart.mockResolvedValue(cartWithOneItem());
    quotationsApi.submitCart.mockResolvedValue({ ...EMPTY_CART, reference_number: "QT-000010" });
    renderQuotations();

    const submitBtn = await screen.findByRole("button", { name: /submit quotation/i });
    await user.click(submitBtn);

    await waitFor(() => expect(quotationsApi.submitCart).toHaveBeenCalled());
    expect(await screen.findByText("QT-000009")).toBeInTheDocument();
  });

  it("fetches the catalog server-side with pagination params, and re-fetches on search/rows-per-page change", async () => {
    const user = userEvent.setup();
    renderQuotations();

    await screen.findByText("Motorola APX 8000");
    expect(quotationsApi.catalogPage).toHaveBeenCalledWith(5, 0, "");

    await user.type(screen.getByPlaceholderText(/search catalog/i), "vortex");
    await waitFor(() => expect(quotationsApi.catalogPage).toHaveBeenLastCalledWith(5, 0, "vortex"));

    await user.selectOptions(screen.getByRole("combobox"), "25");
    await waitFor(() => expect(quotationsApi.catalogPage).toHaveBeenLastCalledWith(25, 0, "vortex"));
  });

  it("does not show an Assign Quote button for a plain customer/staff account", async () => {
    renderQuotations();

    await screen.findByText("Motorola APX 8000");
    expect(screen.queryByRole("button", { name: /assign quote/i })).not.toBeInTheDocument();
  });

  it("lets a manager/admin assign their collated cart to a user", async () => {
    const user = userEvent.setup();
    useAuthMock.mockReturnValue({
      user: { name: "Manager Mo", email: "mo@example.com", role: "manager" } satisfies AuthUser,
      demo: false,
    });
    quotationsApi.myCart.mockResolvedValue({ ...cartWithOneItem(), id: 42 });
    usersApi.list.mockResolvedValue({ items: [{ id: 7, name: "Casey Customer", email: "casey@example.com", role: "customer" }], total: 1, limit: 8, offset: 0 });
    quotationsApi.assign.mockResolvedValue({ ...cartWithOneItem(), id: 42 });
    renderQuotations();

    const assignBtn = await screen.findByRole("button", { name: /assign quote/i });
    await user.click(assignBtn);
    await user.type(screen.getByPlaceholderText(/search staff or customers/i), "casey");

    const match = await screen.findByText("Casey Customer");
    await user.click(match);

    await waitFor(() => expect(quotationsApi.assign).toHaveBeenCalledWith(42, { assignee_type: "user", user_id: 7 }));
    expect(await screen.findByText(/assigned to casey customer/i)).toBeInTheDocument();
  });
});
