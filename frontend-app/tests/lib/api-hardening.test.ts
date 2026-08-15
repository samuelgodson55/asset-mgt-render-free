import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { assetsApi, myItemsApi } from "../../src/lib/api";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("server-side filter contracts", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => vi.unstubAllGlobals());

  it("sends Inventory stock status to the backend instead of filtering only the current page", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ items: [], total: 0, limit: 5, offset: 10 }));

    await assetsApi.list(5, 10, "fx", "camera", "low");

    const [url] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("limit=5");
    expect(String(url)).toContain("offset=10");
    expect(String(url)).toContain("search=fx");
    expect(String(url)).toContain("category=camera");
    expect(String(url)).toContain("status=low");
  });

  it("sends My Items overdue/due-soon filters before pagination", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ name: "Test", assigned_items: [], total: 0, limit: 5, offset: 10 }));

    await myItemsApi.list(5, 10, "overdue");

    const [url] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("limit=5");
    expect(String(url)).toContain("offset=10");
    expect(String(url)).toContain("filter=overdue");
  });
});
