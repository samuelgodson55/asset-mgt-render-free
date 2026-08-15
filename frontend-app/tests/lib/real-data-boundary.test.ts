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
    fetchMock.mockRejectedValueOnce(new TypeError("network error"));
    await expect(api.getAssets()).rejects.toThrow("network error");
  });

  it("uses demo data only when demo mode was explicitly selected", async () => {
    sessionStorage.setItem("ledger:demo-mode", "1");
    fetchMock.mockRejectedValueOnce(new TypeError("network error"));
    const assets = await api.getAssets();
    expect(assets.length).toBeGreaterThan(0);
  });

  it("never turns a failed real dashboard activity request into an empty business dataset", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ items: [], total: 0 }))
      .mockResolvedValueOnce(jsonResponse({ assigned_items: [] }))
      .mockResolvedValueOnce(jsonResponse({ assigned_items: [] }))
      .mockRejectedValueOnce(new TypeError("activity endpoint failed"));

    await expect(api.getStats(false)).rejects.toThrow("activity endpoint failed");
  });
});
