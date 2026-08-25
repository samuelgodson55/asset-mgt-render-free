/**
 * lib/api/core.ts
 * ------------------
 * The shared HTTP client: base URL resolution, demo-mode detection, error
 * types, retry policy, the low-level rawFetch/rawFetchMultipart wrappers
 * every domain module below builds on, and tryLoad (demo-mode fallback to
 * bundled mock data). Also DirectoryPage<T> and qs(), the small shared
 * shape/helper used by every paginated list endpoint.
 */

import { reportClientError, setLastRequestId } from "../errorbeacon";
import { runBusinessOperation, telemetryFetch, type BrowserTelemetrySpan } from "../browserTelemetry";

// Points at the FastAPI backend. In production this app is built with
// `base: '/'` and served (as its own standalone image, frontend/Dockerfile's
// `frontend-react-only` target) by the same nginx that proxies `/api/*` to
// the backend (see nginx/default.react.conf.template's `location /api/`
// block), so the default here is a same-origin relative path -- no CORS, and the
// httpOnly `access_token` session cookie set by POST /auth/login is sent
// automatically. For local `npm run dev` against a bare `uvicorn main:app`
// on another port, set VITE_API_BASE_URL in `.env.local` instead (see
// README.md).
export const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "/api").replace(/\/$/, "");

// Session-scoped (not localStorage) so closing the tab drops back to a
// real sign-in prompt next time, rather than a demo choice persisting
// indefinitely. Shared with lib/auth.tsx (the only other place that reads
// or sets it) so the two files can never drift onto two different keys.
export const DEMO_FLAG_KEY = "ledger:demo-mode";
export function isDemoMode(): boolean {
  return sessionStorage.getItem(DEMO_FLAG_KEY) === "1";
}

// Tracks whether the most recent real request actually reached the
// backend, purely to drive the "Live" / "Demo data" indicator in
// Layout.tsx -- it never gates whether a request is attempted (a 403 on
// one privileged endpoint shouldn't stop other endpoints, which may be
// allowed for the current role, from being tried for real).
export let backendReachable: boolean | null = null;

export class ApiError extends Error {
  status: number;
  /** X-Request-ID / request_id correlation ID from the failed response, when
   *  one was present -- lets UI-layer error handling (see lib/errors.ts)
   *  show the person something they can hand to support, instead of that
   *  ID only ever reaching ErrorBeacon telemetry (see errorbeacon.ts). */
  requestId: string | null;
  constructor(status: number, message: string, requestId: string | null = null) {
    super(message);
    this.status = status;
    this.requestId = requestId;
  }
}

export class MaintenanceModeError extends ApiError {
  constructor(message = "The application is currently undergoing maintenance.", requestId: string | null = null) { super(503, message, requestId); this.name = "MaintenanceModeError"; }
}


/**
 * FastAPI error bodies aren't always `{ detail: string }`. Validation
 * failures (422s) come back as `{ detail: [{ loc, msg, type }, ...] }`,
 * and some handlers return `{ detail: { ... } }`. Passed straight into
 * `new Error(...)`, anything non-string silently becomes the literal
 * string "[object Object]" -- so this always resolves to readable text.
 */
function extractErrorMessage(body: unknown, fallback: string): string {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (detail == null) return fallback;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const msgs = detail
      .map((item) => (item && typeof item === "object" && "msg" in item ? String((item as { msg: unknown }).msg) : null))
      .filter((m): m is string => !!m);
    if (msgs.length) return msgs.join(" ");
  }
  try {
    return JSON.stringify(detail);
  } catch {
    return fallback;
  }
}

const TRANSIENT_HTTP_STATUSES = new Set([502, 503, 504]);
const SAFE_RETRY_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);
const API_RETRY_ATTEMPTS = 3;
const API_RETRY_BASE_DELAY_MS = 150;

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function retryDelayMs(attempt: number): number {
  const jitter = Math.floor(Math.random() * 75);
  return API_RETRY_BASE_DELAY_MS * 2 ** attempt + jitter;
}

async function rawFetchInternal<T = unknown>(path: string, init?: RequestInit, parentSpan?: BrowserTelemetrySpan | null): Promise<T> {
  const method = String(init?.method ?? "GET").toUpperCase();
  const retrySafe = SAFE_RETRY_METHODS.has(method);
  let lastNetworkError: unknown = null;

  for (let attempt = 0; attempt < (retrySafe ? API_RETRY_ATTEMPTS : 1); attempt += 1) {
    let res: Response;
    try {
      res = await telemetryFetch(`${API_BASE}${path}`, {
        credentials: "include",
        headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
        ...init,
      }, parentSpan);
    } catch (err) {
      // A transport/network failure is already ambiguous from the browser's
      // point of view: the request may have left the machine even if no
      // response came back. Do not automatically replay it, even for GETs.
      // HTTP 502/503/504 responses are the only failures this client retries,
      // because those are explicit server/proxy responses that can be safely
      // classified by the policy below. This also preserves the ORIGINAL
      // network error for callers instead of allowing a later retry/test stub
      // failure to replace it.
      lastNetworkError = err;
      reportClientError(err, { source: "api.network", endpoint: path, attempts: attempt + 1 });
      throw err;
    }

    // A real browser `fetch()` always resolves with a Response. Treat an
    // undefined/null value defensively anyway because test doubles, custom
    // fetch adapters, or future wrappers can violate that contract. Most
    // importantly, never turn the original network error into a secondary
    // `Cannot read properties of undefined (reading 'headers')` error.
    if (!res) {
      throw lastNetworkError instanceof Error
        ? lastNetworkError
        : new Error("Request failed: fetch returned no response");
    }

    const requestId = res.headers.get("x-request-id");
    if (requestId) setLastRequestId(requestId);

    // Planned maintenance is an expected application state, not a transient outage.
    // Detect it before the generic 503 retry path so open clients do not hammer the API.
    if (res.status === 503) {
      try {
        const body = await res.clone().json();
        if (body?.code === "MAINTENANCE_MODE") {
          if (typeof window !== "undefined") window.dispatchEvent(new Event("asset-app:maintenance"));
          throw new MaintenanceModeError(extractErrorMessage(body, "The application is currently undergoing maintenance."), requestId || body?.request_id || null);
        }
      } catch (err) {
        if (err instanceof MaintenanceModeError) throw err;
      }
    }

    // Safe reads may survive a brief proxy/container restart. Never retry
    // mutations: a POST/PUT/PATCH/DELETE may already have changed state.
    if (retrySafe && TRANSIENT_HTTP_STATUSES.has(res.status) && attempt + 1 < API_RETRY_ATTEMPTS) {
      await sleep(retryDelayMs(attempt));
      continue;
    }

    if (!res.ok) {
      const fallback = res.statusText || `Request failed (${res.status})`;
      let message = fallback;
      let bodyRequestId: string | null = null;
      try {
        const body = await res.json();
        message = extractErrorMessage(body, fallback);
        if (body && typeof body === "object" && typeof (body as { request_id?: unknown }).request_id === "string") {
          bodyRequestId = (body as { request_id: string }).request_id;
        }
      } catch {
        // body wasn't JSON -- keep the status text
      }
      if (res.status >= 500) reportClientError(new Error(message), { source: "api.http", endpoint: path, status: res.status });
      // A 401 in demo mode is expected on every request -- there's no backend
      // session at all, and tryLoad's fallback to bundled mock data is the
      // intended handling for it. Dispatching auth-expired here anyway is
      // what causes "Continue with demo data" to bounce straight back to
      // Login: the very first authenticated call any page makes 401s, the
      // event fires, and auth.tsx's handleAuthExpired clears demo mode.
      if (res.status === 401 && !isDemoMode() && !path.startsWith("/auth/login") && !path.startsWith("/auth/mfa") && typeof window !== "undefined") {
        window.dispatchEvent(new Event("asset-app:auth-expired"));
      }
      throw new ApiError(res.status, message, bodyRequestId || requestId || null);
    }
    if (res.status === 204) return null as T;
    return (await res.json()) as T;
  }

  throw lastNetworkError instanceof Error ? lastNetworkError : new Error("Request failed after retries");
}

const BUSINESS_OPERATION_RULES: Array<{ method: string; pattern: RegExp; operation: string }> = [
  { method: "POST", pattern: /^\/auth\/login$/, operation: "auth.login" },
  { method: "POST", pattern: /^\/auth\/mfa\/verify$/, operation: "auth.mfa.verify" },
  { method: "POST", pattern: /^\/auth\/mfa\/setup\/confirm$/, operation: "auth.mfa.setup" },
  { method: "POST", pattern: /^\/auth\/logout$/, operation: "auth.logout" },
  { method: "POST", pattern: /^\/auth\/forgot-password$/, operation: "auth.password.forgot" },
  { method: "POST", pattern: /^\/auth\/reset-password$/, operation: "auth.password.reset" },
  { method: "POST", pattern: /^\/auth\/update-password$/, operation: "auth.password.update" },
  { method: "POST", pattern: /^\/auth\/mfa\/recovery-codes\/regenerate$/, operation: "auth.mfa.recovery_codes.regenerate" },
  { method: "PATCH", pattern: /^\/auth\/me$/, operation: "profile.update" },

  { method: "POST", pattern: /^\/assets\/\d+\/checkout_advanced$/, operation: "checkout.complete" },
  { method: "POST", pattern: /^\/assets\/\d+\/checkin$/, operation: "checkin.complete" },
  { method: "POST", pattern: /^\/assets\/\d+\/exception$/, operation: "asset.exception.flag" },
  { method: "POST", pattern: /^\/assets\/\d+\/exception\/\d+\/recall$/, operation: "asset.exception.recall" },
  { method: "POST", pattern: /^\/assets\/\d+\/restore$/, operation: "asset.restore" },
  { method: "POST", pattern: /^\/assets\/\d+\/purge$/, operation: "asset.purge" },
  { method: "POST", pattern: /^\/assets$/, operation: "asset.create" },
  { method: "PUT", pattern: /^\/assets\/\d+\/quantity$/, operation: "asset.quantity.update" },
  { method: "PUT", pattern: /^\/assets\/\d+\/name$/, operation: "asset.name.update" },
  { method: "PUT", pattern: /^\/assets\/\d+\/category$/, operation: "asset.category.update" },
  { method: "PUT", pattern: /^\/assets\/\d+\/department$/, operation: "asset.department.update" },
  { method: "PUT", pattern: /^\/assets\/\d+\/price$/, operation: "asset.price.update" },
  { method: "DELETE", pattern: /^\/assets\/\d+$/, operation: "asset.delete" },
  { method: "POST", pattern: /^\/assets\/import$/, operation: "asset.import" },

  { method: "POST", pattern: /^\/checkouts\/\d+\/return$/, operation: "checkin.complete" },
  { method: "POST", pattern: /^\/checkouts\/\d+\/extend$/, operation: "checkout.extend" },
  { method: "POST", pattern: /^\/checkouts\/bulk-extend$/, operation: "checkout.extend.bulk" },
  { method: "POST", pattern: /^\/checkouts\/\d+\/extension-requests$/, operation: "checkout.extension.request" },
  { method: "POST", pattern: /^\/checkouts\/extension-requests\/\d+\/decision$/, operation: "checkout.extension.decide" },

  { method: "POST", pattern: /^\/users$/, operation: "user.create" },
  { method: "PATCH", pattern: /^\/users\/\d+$/, operation: "user.update" },
  { method: "DELETE", pattern: /^\/users\/\d+$/, operation: "user.delete" },
  { method: "POST", pattern: /^\/users\/\d+\/restore$/, operation: "user.restore" },
  { method: "POST", pattern: /^\/users\/\d+\/purge$/, operation: "user.purge" },
  { method: "POST", pattern: /^\/users\/\d+\/convert-to-outsider$/, operation: "user.convert_to_outsider" },
  { method: "POST", pattern: /^\/users\/\d+\/reset-password$/, operation: "user.password.reset" },

  { method: "PATCH", pattern: /^\/outsiders\/\d+$/, operation: "outsider.update" },
  { method: "DELETE", pattern: /^\/outsiders\/\d+$/, operation: "outsider.delete" },
  { method: "POST", pattern: /^\/outsiders\/\d+\/convert-to-user$/, operation: "outsider.convert_to_user" },

  { method: "POST", pattern: /^\/quotations\/items$/, operation: "quote.item.add" },
  { method: "PUT", pattern: /^\/quotations\/items\/\d+$/, operation: "quote.item.update" },
  { method: "DELETE", pattern: /^\/quotations\/items\/\d+$/, operation: "quote.item.remove" },
  { method: "POST", pattern: /^\/quotations\/submit$/, operation: "quote.submit" },
  { method: "POST", pattern: /^\/quotations\/\d+\/approve$/, operation: "quote.approve" },
  { method: "POST", pattern: /^\/quotations\/\d+\/checkout$/, operation: "quote.checkout" },
  { method: "POST", pattern: /^\/quotations\/\d+\/paid$/, operation: "quote.paid" },
  { method: "POST", pattern: /^\/quotations\/\d+\/assign$/, operation: "quote.assign" },
  { method: "POST", pattern: /^\/quotations\/\d+\/outsourced-items$/, operation: "quote.outsourced_item.add" },
  { method: "DELETE", pattern: /^\/quotations\/\d+\/outsourced-items\/\d+$/, operation: "quote.outsourced_item.remove" },
  { method: "POST", pattern: /^\/quotations$/, operation: "quote.create" },
  { method: "PUT", pattern: /^\/quotations\/\d+\/discount$/, operation: "quote.discount.update" },
  { method: "PUT", pattern: /^\/quotations\/\d+$/, operation: "quote.update" },
  { method: "DELETE", pattern: /^\/quotations\/\d+$/, operation: "quote.delete" },
  { method: "POST", pattern: /^\/quotations\/me\/\d+\/items$/, operation: "quote.my_item.add" },
  { method: "PUT", pattern: /^\/quotations\/me\/\d+\/items\/\d+$/, operation: "quote.my_item.update" },
  { method: "DELETE", pattern: /^\/quotations\/me\/\d+\/items\/\d+$/, operation: "quote.my_item.remove" },
  { method: "POST", pattern: /^\/quotations\/me\/notifications\/read$/, operation: "quote.notifications.read" },

  { method: "POST", pattern: /^\/backup\/create$/, operation: "backup.create" },
  { method: "POST", pattern: /^\/backup\/restore\/[^/]+$/, operation: "backup.restore" },
  { method: "POST", pattern: /^\/backup\/restore-upload$/, operation: "backup.restore.upload" },
  { method: "DELETE", pattern: /^\/backup\/[^/]+$/, operation: "backup.delete" },

  { method: "POST", pattern: /^\/audit-logs\/export$/, operation: "audit.export.start" },
  { method: "PUT", pattern: /^\/maintenance\/status$/, operation: "maintenance.update" },
  { method: "PUT", pattern: /^\/settings\/digest-recipients$/, operation: "settings.digest.update" },
  { method: "PUT", pattern: /^\/settings\/vat$/, operation: "settings.vat.update" },
];

function businessOperationForRequest(method: string, path: string): string | null {
  const cleanPath = path.split("?", 1)[0];
  return BUSINESS_OPERATION_RULES.find(
    (rule) => rule.method === method && rule.pattern.test(cleanPath),
  )?.operation ?? null;
}

export async function rawFetch<T = unknown>(path: string, init?: RequestInit): Promise<T> {
  const method = String(init?.method ?? "GET").toUpperCase();
  const operation = businessOperationForRequest(method, path);

  if (!operation) return rawFetchInternal<T>(path, init);

  return runBusinessOperation(
    operation,
    (parentSpan) => rawFetchInternal<T>(path, init, parentSpan),
    [],
  );
}

/**
 * Same contract as rawFetch, but for multipart/form-data bodies (file
 * uploads) -- deliberately does NOT set a Content-Type header itself, so
 * the browser can attach its own `multipart/form-data; boundary=...`
 * value, which a hardcoded `application/json` (rawFetch's default) would
 * clobber and break.
 */
async function rawFetchMultipartInternal<T = unknown>(path: string, formData: FormData, init?: RequestInit, parentSpan?: BrowserTelemetrySpan | null): Promise<T> {
  const res = await telemetryFetch(`${API_BASE}${path}`, {
    method: "POST",
    credentials: "include",
    body: formData,
    ...init,
  }, parentSpan);
  const requestId = res.headers.get("x-request-id");
  if (requestId) setLastRequestId(requestId);
  if (!res.ok) {
    const fallback = res.statusText || `Request failed (${res.status})`;
    let message = fallback;
    let bodyRequestId: string | null = null;
    try {
      const body = await res.json();
      message = extractErrorMessage(body, fallback);
      if (body && typeof body === "object" && typeof (body as { request_id?: unknown }).request_id === "string") {
        bodyRequestId = (body as { request_id: string }).request_id;
      }
    } catch {
      // body wasn't JSON -- keep the status text
    }
    if (res.status === 401 && !isDemoMode() && !path.startsWith("/auth/login") && !path.startsWith("/auth/mfa") && typeof window !== "undefined") {
      window.dispatchEvent(new Event("asset-app:auth-expired"));
    }
    throw new ApiError(res.status, message, bodyRequestId || requestId || null);
  }
  return (await res.json()) as T;
}

export async function rawFetchMultipart<T = unknown>(
  path: string,
  formData: FormData,
  init?: RequestInit,
): Promise<T> {
  const method = String(init?.method ?? "POST").toUpperCase();
  const operation = businessOperationForRequest(method, path);

  if (!operation) return rawFetchMultipartInternal<T>(path, formData, init);

  return runBusinessOperation(
    operation,
    (parentSpan) => rawFetchMultipartInternal<T>(path, formData, init, parentSpan),
    [],
  );
}

/**
 * Runs a real loader. In genuine demo mode (no backend session at all --
 * see DEMO_FLAG_KEY / continueAsDemo() in lib/auth.tsx) any failure falls
 * back to the bundled demo dataset so the UI stays fully explorable
 * without a backend.
 *
 * For a REAL signed-in account, `fallback` is never used: a failure (network
 * drop, 401 from an expired session, a 500, anything) is re-thrown instead.
 * The whole point of tryLoad's fallback is a self-contained demo experience
 * -- silently substituting fabricated demo data into a real account's view
 * on a transient backend hiccup would be far worse than an empty/error
 * state, since the person has no way to tell the numbers/rows in front of
 * them aren't real. Demo data and real backend data must never merge.
 */
export async function tryLoad<T>(loader: () => Promise<T>, fallback: T): Promise<T> {
  try {
    const data = await loader();
    backendReachable = true;
    return data;
  } catch (err) {
    if (isDemoMode()) {
      backendReachable = false;
      return fallback;
    }
    backendReachable = false;
    throw err;
  }
}

export interface DirectoryPage<T> { items: T[]; total: number; limit: number; offset: number }

export function qs(params: Record<string, string | number | undefined>): string {
  const parts = Object.entries(params).filter(([, v]) => v !== undefined && v !== "");
  return parts.length ? `?${parts.map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`).join("&")}` : "";
}

