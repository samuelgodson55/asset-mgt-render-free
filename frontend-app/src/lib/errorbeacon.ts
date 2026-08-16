// Lightweight client-side error reporter. Catches uncaught exceptions and
// unhandled promise rejections in the browser and forwards them to the
// backend's /api/telemetry/client-error endpoint, which in turn feeds
// ErrorBeacon (see errorbeacon/errorbeacon-telegram-setup.md). This module
// never talks to ErrorBeacon directly -- the backend is the only thing
// holding the ERRORBEACON_API_KEY.

const SENSITIVE_QUERY_PARAM = /^(token|access_token|refresh_token|reset_token|code|secret|key|password|passwd|api_key|apikey|session|session_id)$/i;
const REPORT_THROTTLE_MS = 1000;

let lastReportAt = 0;
let lastRequestId: string | null = null;
let patched = false;

// Strips sensitive query-string values and any hash fragment before a URL is
// sent along with an error report.
function sanitizeUrl(url: string): string {
  try {
    const u = new URL(url, window.location.origin);
    [...u.searchParams.keys()].forEach((key) => {
      if (SENSITIVE_QUERY_PARAM.test(key)) u.searchParams.set(key, "[REDACTED]");
    });
    u.hash = "";
    return u.toString().slice(0, 2000);
  } catch {
    return String(url || "").slice(0, 2000);
  }
}

// The backend stamps every response with an X-Request-ID header (see
// installGlobalErrorBeacon() below); we remember the most recent one so any
// client error reported shortly after can be correlated with the request
// that likely caused it.
export function setLastRequestId(v: string | null | undefined): void {
  if (v) lastRequestId = String(v).slice(0, 200);
}

export function getLastRequestId(): string | null {
  return lastRequestId;
}

// Reports a single client-side error to the backend. Throttled to at most
// one report per second so a tight error loop (e.g. a render error that
// re-throws every frame) can't flood the endpoint.
export function reportClientError(error: unknown, context: Record<string, unknown> = {}): void {
  if (typeof window === "undefined") return;

  const now = Date.now();
  if (now - lastReportAt < REPORT_THROTTLE_MS) return;
  lastReportAt = now;

  const err = error instanceof Error ? error : new Error(String(error));
  fetch("/api/telemetry/client-error", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    keepalive: true,
    body: JSON.stringify({
      message: String(err.message || err).slice(0, 5000),
      stack: String(err.stack || "").slice(0, 12000),
      path: window.location.pathname,
      request_id: lastRequestId,
      context: {
        ...context,
        url: sanitizeUrl(window.location.href),
        userAgent: navigator.userAgent.slice(0, 500),
        requestId: lastRequestId,
      },
    }),
  }).catch(() => {
    // Best-effort only -- a failed error report should never itself surface
    // as an error to the user.
  });
}

// Wires window-level error listeners and wraps window.fetch so every
// response's X-Request-ID header updates lastRequestId. Safe to call more
// than once; only the first call takes effect.
export function installGlobalErrorBeacon(): void {
  if (typeof window === "undefined" || patched) return;
  patched = true;

  const nativeFetch = window.fetch.bind(window);
  window.fetch = async (...args: Parameters<typeof fetch>) => {
    const response = await nativeFetch(...args);
    const requestId = response.headers.get("x-request-id");
    if (requestId) setLastRequestId(requestId);
    return response;
  };

  window.addEventListener("error", (e) => {
    reportClientError(e.error || new Error(e.message), { source: "window.error" });
  });
  window.addEventListener("unhandledrejection", (e) => {
    reportClientError(e.reason || new Error("Unhandled promise rejection"), { source: "unhandledrejection" });
  });
}
