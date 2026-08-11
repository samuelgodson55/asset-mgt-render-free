import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { quotationsApi, formatPrice, setCurrencyCode, ApiError, DEMO_FLAG_KEY } from "../../src/lib/api";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("quotationsApi", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    sessionStorage.removeItem(DEMO_FLAG_KEY);
  });

  it("addToCart posts the asset/quantity/date range to /quotations/items", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ items: [], subtotal: 0, vat_percent: 7.5, vat_amount: 0, total: 0 }));

    await quotationsApi.addToCart(7, 2, "2026-08-10", "2026-08-12");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/quotations/items");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(init!.body as string)).toEqual({
      asset_id: 7,
      quantity: 2,
      start_date: "2026-08-10",
      due_date: "2026-08-12",
    });
  });

  it("updateCartItem PUTs the new quantity to /quotations/items/:id", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ items: [], subtotal: 0, vat_percent: 0, vat_amount: 0, total: 0 }));

    await quotationsApi.updateCartItem(42, 5);

    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/quotations/items/42");
    expect(init?.method).toBe("PUT");
    expect(JSON.parse(init!.body as string)).toEqual({ quantity: 5 });
  });

  it("removeCartItem DELETEs /quotations/items/:id", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ items: [], subtotal: 0, vat_percent: 0, vat_amount: 0, total: 0 }));

    await quotationsApi.removeCartItem(9);

    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/quotations/items/9");
    expect(init?.method).toBe("DELETE");
  });

  it("list encodes pagination + search as a query string", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ items: [], total: 0, limit: 20, offset: 0 }));

    await quotationsApi.list(20, 40, "acme corp");

    const [url] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/quotations?");
    expect(String(url)).toContain("limit=20");
    expect(String(url)).toContain("offset=40");
    expect(String(url)).toContain("search=acme%20corp");
  });

  it("myCart rejects (does NOT fall back to demo data) for a real session when the backend is unreachable", async () => {
    // No demo flag set here -- this is the real-account path. Silently
    // substituting the bundled demo cart for a real, signed-in session's
    // cart on a network failure is exactly the bug this test used to
    // assert as correct; it must now surface the failure instead.
    fetchMock.mockRejectedValueOnce(new TypeError("network error"));

    await expect(quotationsApi.myCart()).rejects.toThrow("network error");
  });

  it("myCart falls back to demo data only in genuine demo mode", async () => {
    sessionStorage.setItem(DEMO_FLAG_KEY, "1");
    fetchMock.mockRejectedValueOnce(new TypeError("network error"));

    const cart = await quotationsApi.myCart();

    expect(cart.items).toEqual([]);
    expect(cart.total).toBe(0);
  });

  it("approve throws an ApiError with the backend's detail message on failure", async () => {
    fetchMock.mockImplementation(async () => jsonResponse({ detail: "Quotation is not pending review." }, 409));

    await expect(quotationsApi.approve(1)).rejects.toBeInstanceOf(ApiError);
    await expect(quotationsApi.approve(1)).rejects.toThrow("Quotation is not pending review.");
  });

  it("checkout sends an empty outsource_shortfall_items list by default", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ message: "Checked out." }));

    await quotationsApi.checkout(5);

    const [, init] = fetchMock.mock.calls[0];
    expect(JSON.parse(init!.body as string)).toEqual({ outsource_shortfall_items: [] });
  });
});

describe("formatPrice / setCurrencyCode", () => {
  afterEach(() => setCurrencyCode("NGN"));

  it("formats a number using the active currency code", () => {
    setCurrencyCode("USD");
    expect(formatPrice(1234.5)).toMatch(/\$/);
  });

  it("returns an em dash for null/undefined", () => {
    expect(formatPrice(null)).toBe("—");
    expect(formatPrice(undefined)).toBe("—");
  });

  it("falls back to a plain string for an unsupported currency code", () => {
    setCurrencyCode("NOT_A_CODE");
    expect(formatPrice(10)).toBe("NOT_A_CODE 10.00");
  });
});
