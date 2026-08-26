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

describe("request header merging", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("keeps the default Content-Type when a caller passes an extra header (e.g. Idempotency-Key), instead of the extra header replacing the whole headers object", async () => {
    // Regression test: rawFetchInternal used to spread `...init` AFTER a
    // pre-computed `headers` object, so any caller-supplied `headers` key
    // (added for e.g. the Idempotency-Key -- see lib/idempotency.ts)
    // completely OVERWROTE the computed headers instead of merging with
    // them, silently dropping Content-Type from every JSON POST that also
    // sent a custom header.
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse({ ok: true }));
    vi.stubGlobal("fetch", fetchMock);

    const { rawFetch } = await import("../../src/lib/api/core");
    await rawFetch("/assets/1/checkout_advanced", {
      method: "POST",
      body: JSON.stringify({ quantity: 1 }),
      headers: { "Idempotency-Key": "test-key-123" },
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [, requestInit] = fetchMock.mock.calls[0] as [string, RequestInit];
    const headers = requestInit.headers as Record<string, string>;
    expect(headers["Content-Type"]).toBe("application/json");
    expect(headers["Idempotency-Key"]).toBe("test-key-123");
  });
});
