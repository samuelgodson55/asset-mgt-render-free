// =============================================================================
// js/errorbeacon.js
// -----------------------------------------------------------------------------
// Lightweight client-side error reporter. Catches uncaught exceptions and
// unhandled promise rejections in the browser and forwards them to the
// backend's /api/telemetry/client-error endpoint, which in turn feeds
// ErrorBeacon (see errorbeacon/errorbeacon-telegram-setup.md). This module
// never talks to ErrorBeacon directly -- the backend is the only thing
// holding the ERRORBEACON_INGEST_API_KEY.
// =============================================================================

const SENSITIVE_QUERY_PARAM = /^(token|access_token|refresh_token|reset_token|code|secret|key|password|passwd|api_key|apikey|session|session_id)$/i;
const REPORT_THROTTLE_MS = 1000;

let lastReportAt = 0;
let lastRequestId = null;
let patched = false;

// Strips sensitive query-string values and any hash fragment before a URL is
// sent along with an error report.
function sanitizeUrl(url) {
  try {
    const u = new URL(String(url), window.location.origin);
    [...u.searchParams.keys()].forEach((key) => {
      if (SENSITIVE_QUERY_PARAM.test(key)) u.searchParams.set(key, '[REDACTED]');
    });
    u.hash = '';
    return u.toString().slice(0, 2000);
  } catch {
    return String(url || '').slice(0, 2000);
  }
}

// The backend stamps every response with an X-Request-ID header (see
// installGlobalErrorBeacon() below); we remember the most recent one so any
// client error reported shortly after can be correlated with the request
// that likely caused it.
export function setLastRequestId(v) {
  if (v) lastRequestId = String(v).slice(0, 200);
}

export function getLastRequestId() {
  return lastRequestId;
}

// Reports a single client-side error to the backend. Throttled to at most
// one report per second so a tight error loop (e.g. a render error that
// re-throws every frame) can't flood the endpoint.
export function reportClientError(error, context = {}) {
  if (typeof window === 'undefined') return;

  const now = Date.now();
  if (now - lastReportAt < REPORT_THROTTLE_MS) return;
  lastReportAt = now;

  const err = error instanceof Error ? error : new Error(String(error));
  const payload = JSON.stringify({
    message: String(err.message || err).slice(0, 5000),
    stack: String(err.stack || '').slice(0, 12000),
    path: window.location.pathname,
    request_id: lastRequestId,
    context: {
      ...context,
      url: sanitizeUrl(window.location.href),
      userAgent: navigator.userAgent.slice(0, 500),
      requestId: lastRequestId,
    },
  });

  // IMPORTANT: telemetry must never use the application's global fetch().
  // API tests intentionally mock fetch() and expect exactly one business
  // request. More importantly, a failed telemetry request must never replace
  // the original application/network error we are trying to report.
  try {
    if (typeof navigator !== 'undefined' && typeof navigator.sendBeacon === 'function') {
      const accepted = navigator.sendBeacon(
        '/api/telemetry/client-error',
        new Blob([payload], { type: 'application/json' }),
      );
      if (accepted) return;
    }

    // sendBeacon is unavailable or declined the payload. XHR is deliberately
    // used as an isolated fallback instead of fetch(), so telemetry remains
    // outside the application's request/retry/error path.
    if (typeof XMLHttpRequest === 'function') {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/api/telemetry/client-error', true);
      xhr.setRequestHeader('Content-Type', 'application/json');
      xhr.withCredentials = true;
      xhr.send(payload);
    }
  } catch {
    // Telemetry is strictly best-effort. Never throw from this function.
  }
}

// Wires only window-level error listeners. API request correlation is handled
// by the API module itself, immediately after each successful fetch. We
// deliberately do NOT monkey-patch window.fetch here: changing the global
// fetch function makes request failures harder to reason about and can cause
// test doubles or other application code to receive an unexpected wrapper.
export function installGlobalErrorBeacon() {
  if (patched || typeof window === 'undefined') return;
  patched = true;

  window.addEventListener('error', (e) => {
    reportClientError(e.error || new Error(e.message), { source: 'window.error' });
  });
  window.addEventListener('unhandledrejection', (e) => {
    reportClientError(e.reason || new Error('Unhandled promise rejection'), { source: 'unhandledrejection' });
  });
}
