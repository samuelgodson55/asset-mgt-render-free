// Guards the boundary between "demo mode" (src/lib/mock.ts's fallback
// data, only used when the user has explicitly opted into it) and a real,
// authenticated session against the live backend. The failure mode this
// suite exists to prevent: a real user's session silently falling back to
// fabricated demo data when a backend request fails, which would make a
// genuine outage look like an empty-but-working app instead of a visible
// error.
import { describe, expect, it, beforeEach, vi } from "vitest";
import { api } from "../../src/lib/api";

const fetchMock = vi.fn();
vi.stubGlobal("fetch", fetchMock);

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

describe("authenticated data boundary", () => {
  beforeEach(() => {
    sessionStorage.clear();
    fetchMock.mockReset();
  });

  it("never substitutes mock assets for a real session when the backend fails", async () => {
    // No demo-mode flag set -- a network failure here must propagate as a
    // real error, not get quietly papered over with fake asset rows.
    fetchMock.mockRejectedValueOnce(new TypeError("network error"));
    await expect(api.getAssets()).rejects.toThrow("network error");
  });

  it("uses demo data only when demo mode was explicitly selected", async () => {
    // With the demo-mode flag set, the SAME network failure should now
    // resolve to non-empty mock data instead of throwing -- this is the
    // one and only condition under which fallback data is acceptable.
    sessionStorage.setItem("ledger:demo-mode", "1");
    fetchMock.mockRejectedValueOnce(new TypeError("network error"));
    const assets = await api.getAssets();
    expect(assets.length).toBeGreaterThan(0);
  });

  it("never turns a failed real dashboard activity request into an empty business dataset", async () => {
    // The first three dashboard fetches succeed; only the final
    // (checkout-activity) call fails. getStats() must still surface that
    // failure rather than quietly returning a stats object with an empty
    // activity series, which would look like "no activity" instead of
    // "this request is broken".
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ items: [], total: 0 }))
      .mockResolvedValueOnce(jsonResponse({ assigned_items: [] }))
      .mockResolvedValueOnce(jsonResponse({ assigned_items: [] }))
      .mockRejectedValueOnce(new TypeError("activity endpoint failed"));

    await expect(api.getStats(false)).rejects.toThrow("activity endpoint failed");
  });
});
