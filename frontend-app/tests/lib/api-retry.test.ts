import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { assetsApi } from "../../src/lib/api";

function jsonResponse(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

describe("safe API retry policy", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("retries a GET after a transient 503 and succeeds", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ detail: "starting" }, 503))
      .mockResolvedValueOnce(jsonResponse({ items: [], total: 0, limit: 10, offset: 0 }, 200, { "x-request-id": "req-recovered" }));
    vi.stubGlobal("fetch", fetchMock);

    const promise = assetsApi.list(10, 0);
    await vi.advanceTimersByTimeAsync(1000);
    const result = await promise;

    expect(result.total).toBe(0);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("does not retry a mutation after a 503", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse({ detail: "starting" }, 503));
    vi.stubGlobal("fetch", fetchMock);

    // Login is a POST and therefore must remain exactly-once from the browser
    // client's perspective. A server/proxy retry is only safe for reads.
    const { auth } = await import("../../src/lib/api");
    await expect(auth.login("someone@example.com", "bad-password")).rejects.toMatchObject({ status: 503 });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
