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
  // Error reporting is deliberately best-effort and must NOT reuse the
  // application's fetch wrapper. Reusing fetch here creates a subtle failure
  // loop: an API request can fail, telemetry tries to use the same mocked or
  // wrapped fetch, and telemetry can then throw while reporting the original
  // error. That second error would hide the real network/server error.
  const body = JSON.stringify({
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
  });

  try {
    // sendBeacon is ideal for telemetry because it is asynchronous and does
    // not consume the application's normal fetch path. It also works during
    // page unload, when a normal fetch may be cancelled.
    if (typeof navigator !== "undefined" && typeof navigator.sendBeacon === "function") {
      const accepted = navigator.sendBeacon(
        "/api/telemetry/client-error",
        new Blob([body], { type: "application/json" }),
      );
      if (accepted) return;
    }

    // Older browsers may not support sendBeacon. XMLHttpRequest is the
    // fallback so telemetry still does not interfere with fetch-based API
    // calls or their tests. The request is intentionally fire-and-forget.
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/telemetry/client-error", true);
    xhr.withCredentials = true;
    xhr.setRequestHeader("Content-Type", "application/json");
    xhr.send(body);
  } catch {
    // A telemetry failure must NEVER replace or alter the original
    // application error. There is deliberately nothing else to throw here.
  }
}

// Wires window-level error listeners and wraps window.fetch so every
// response's X-Request-ID header updates lastRequestId. Safe to call more
// than once; only the first call takes effect.
export function installGlobalErrorBeacon(): void {
  if (typeof window === "undefined" || patched) return;
  patched = true;

  // Do NOT monkey-patch window.fetch here. The API layer owns its request
  // handling and already records X-Request-ID on every response it receives.
  // A global fetch wrapper creates hidden coupling with tests and with other
  // code that deliberately replaces fetch, and it can turn a failed business
  // request into a secondary "response.headers" error. Keeping ErrorBeacon
  // limited to window-level error listeners makes telemetry completely
  // independent from the application's HTTP implementation.

  window.addEventListener("error", (e) => {
    reportClientError(e.error || new Error(e.message), { source: "window.error" });
  });
  window.addEventListener("unhandledrejection", (e) => {
    reportClientError(e.reason || new Error("Unhandled promise rejection"), { source: "unhandledrejection" });
  });
}
