// Guards the "extremely user-friendly errors" behavior described in
// src/lib/errors.ts: a caught request failure should never surface as a
// raw status code or an opaque exception message, and -- when the failed
// response carried a correlation ID (X-Request-ID header or a `request_id`
// field in the JSON body) -- that ID must survive all the way from
// src/lib/api/core.ts's ApiError through to the FriendlyError a page
// actually renders, since it's the one thing support needs to look up the
// matching server-side log line.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { rawFetch, ApiError, MaintenanceModeError } from "../../src/lib/api/core";
import { friendlyError, formatFriendlyErrorLine } from "../../src/lib/errors";

function jsonResponse(body: unknown, status: number, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

describe("friendlyError()", () => {
  it("never shows a raw status code or exception class name as the title", () => {
    for (const status of [400, 401, 403, 404, 409, 429, 500, 503]) {
      const err = new ApiError(status, "Some raw backend detail", null);
      const f = friendlyError(err, "Couldn't complete that action.");
      expect(f.title).not.toMatch(/^\d{3}$/);
      expect(f.title.toLowerCase()).not.toContain("apierror");
    }
  });

  it("marks a 409 conflict as retryable with a plain-language title, keeping the specific backend detail as the message", () => {
    const err = new ApiError(409, "This asset was already assigned to someone else.", "REQ-8F4A21");
    const f = friendlyError(err, "We couldn't complete that checkout.");
    expect(f.title).toBe("We couldn't complete that checkout.");
    expect(f.message).toBe("This asset was already assigned to someone else.");
    expect(f.retryable).toBe(true);
    expect(f.requestId).toBe("REQ-8F4A21");
  });

  it("marks a 403 as not retryable and doesn't imply the person did anything wrong", () => {
    const err = new ApiError(403, "Forbidden", null);
    const f = friendlyError(err, "Couldn't do that.");
    expect(f.retryable).toBe(false);
    expect(f.title.toLowerCase()).toContain("permission");
  });

  it("treats a 401 as a session expiry, not a generic failure", () => {
    const err = new ApiError(401, "Unauthorized", null);
    const f = friendlyError(err, "Couldn't do that.");
    expect(f.title.toLowerCase()).toContain("session");
    expect(f.retryable).toBe(false);
  });

  it("surfaces MaintenanceModeError distinctly from a generic 5xx", () => {
    const err = new MaintenanceModeError("Scheduled maintenance in progress.", "REQ-MAINT-1");
    const f = friendlyError(err, "Couldn't do that.");
    expect(f.title.toLowerCase()).toContain("maintenance");
    expect(f.requestId).toBe("REQ-MAINT-1");
    expect(f.retryable).toBe(true);
  });

  it("treats a fetch()-level failure (no ApiError at all) as a connectivity problem", () => {
    const f = friendlyError(new TypeError("Failed to fetch"), "Couldn't do that.");
    expect(f.title.toLowerCase()).toMatch(/reach|connection|offline/);
    expect(f.retryable).toBe(true);
  });

  it("includes an honest reassurance line by default, and lets callers override it", () => {
    const err = new ApiError(500, "boom", null);
    const withDefault = friendlyError(err, "Couldn't do that.");
    expect(withDefault.reassurance).toBe("Nothing was changed.");

    const custom = friendlyError(err, "Couldn't do that.", { reassurance: "Your cart is still saved -- nothing was lost." });
    expect(custom.reassurance).toBe("Your cart is still saved -- nothing was lost.");
  });
});

describe("formatFriendlyErrorLine()", () => {
  it("appends the reference ID for compact single-line displays, and omits it entirely when there isn't one", () => {
    const withId = friendlyError(new ApiError(500, "boom", "REQ-123"), "Couldn't save.");
    expect(formatFriendlyErrorLine(withId)).toContain("Reference: REQ-123");

    const withoutId = friendlyError(new ApiError(400, "Bad input", null), "Couldn't save.");
    expect(formatFriendlyErrorLine(withoutId)).not.toContain("Reference:");
  });
});

describe("correlation ID propagation from a real failed request", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => vi.unstubAllGlobals());

  it("carries the X-Request-ID response header through ApiError into the FriendlyError support sees", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ detail: "Only 2 units are available." }, 409, { "X-Request-ID": "REQ-HEADER-1" })
    );

    let caught: unknown;
    try {
      await rawFetch("/quotations/cart/submit", { method: "POST" });
    } catch (err) {
      caught = err;
    }

    expect(caught).toBeInstanceOf(ApiError);
    const f = friendlyError(caught, "We couldn't complete that order.");
    expect(f.requestId).toBe("REQ-HEADER-1");
    expect(formatFriendlyErrorLine(f)).toContain("REQ-HEADER-1");
  });

  it("falls back to a request_id in the JSON body when there's no X-Request-ID header", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: "Server error.", request_id: "REQ-BODY-1" }, 500));

    let caught: unknown;
    try {
      await rawFetch("/assets/1", { method: "PATCH" });
    } catch (err) {
      caught = err;
    }

    expect(caught).toBeInstanceOf(ApiError);
    const f = friendlyError(caught, "Couldn't save that change.");
    expect(f.requestId).toBe("REQ-BODY-1");
  });
});
