import { mockAssets, mockCheckouts, mockExtensions, mockNotifications, mockStats, mockBackups, mockBackupStatus, mockDigestRecipients, mockCatalog, mockQuotationCart, mockReportsDashboard } from "./mock";
import type { AssetType, Checkout, ExtensionRequest, NotificationItem, DashboardStats, BackupEntry, BackupStatus, RestoreResult, ImportResult, MyItem, ProfileDetail, UserRow, OutsiderRow, CustodyItem, AuditLogEntry, PublicConfig, CatalogAsset, QuotationCartOrDetail, QuotationListRow, FulfillmentQueueRow, QuotationOutsourcedItemCreate, QuotationOutsourceShortfallItem, AssetDetails, DeletedAssetRow, DeletedUserRow, RosterUser, BulkExtendResult, MyExtensionDecision, QuotationNotification, ReportsDashboard } from "./types";


// Points at the FastAPI backend. In production this app is built with
// `base: '/'` and served (as its own standalone image, frontend/Dockerfile's
// `frontend-react-only` target) by the same nginx that proxies `/api/*` to
// the backend (see nginx/default.react.conf.template's `location /api/`
// block), so the default here is a same-origin relative path -- no CORS, and the
// httpOnly `access_token` session cookie set by POST /auth/login is sent
// automatically. For local `npm run dev` against a bare `uvicorn main:app`
// on another port, set VITE_API_BASE_URL in `.env.local` instead (see
// README.md).
const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "/api").replace(/\/$/, "");

// Session-scoped (not localStorage) so closing the tab drops back to a
// real sign-in prompt next time, rather than a demo choice persisting
// indefinitely. Shared with lib/auth.tsx (the only other place that reads
// or sets it) so the two files can never drift onto two different keys.
export const DEMO_FLAG_KEY = "ledger:demo-mode";

function isDemoMode(): boolean {
  return sessionStorage.getItem(DEMO_FLAG_KEY) === "1";
}

// Tracks whether the most recent real request actually reached the
// backend, purely to drive the "Live" / "Demo data" indicator in
// Layout.tsx -- it never gates whether a request is attempted (a 403 on
// one privileged endpoint shouldn't stop other endpoints, which may be
// allowed for the current role, from being tried for real).
let backendReachable: boolean | null = null;

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
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

async function rawFetch<T = unknown>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    ...init,
  });
  if (!res.ok) {
    const fallback = res.statusText || `Request failed (${res.status})`;
    let message = fallback;
    try {
      const body = await res.json();
      message = extractErrorMessage(body, fallback);
    } catch {
      // body wasn't JSON -- keep the status text
    }
    throw new ApiError(res.status, message);
  }
  if (res.status === 204) return null as T;
  return (await res.json()) as T;
}

/**
 * Same contract as rawFetch, but for multipart/form-data bodies (file
 * uploads) -- deliberately does NOT set a Content-Type header itself, so
 * the browser can attach its own `multipart/form-data; boundary=...`
 * value, which a hardcoded `application/json` (rawFetch's default) would
 * clobber and break.
 */
async function rawFetchMultipart<T = unknown>(path: string, formData: FormData, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    credentials: "include",
    body: formData,
    ...init,
  });
  if (!res.ok) {
    const fallback = res.statusText || `Request failed (${res.status})`;
    let message = fallback;
    try {
      const body = await res.json();
      message = extractErrorMessage(body, fallback);
    } catch {
      // body wasn't JSON -- keep the status text
    }
    throw new ApiError(res.status, message);
  }
  return (await res.json()) as T;
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
async function tryLoad<T>(loader: () => Promise<T>, fallback: T): Promise<T> {
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

/**
 * Like tryLoad, but for genuinely non-sensitive, non-demo UI defaults only
 * (today: just GET /config/public's currency/site-name/stock-visibility
 * flag). Always falls back on failure, demo mode or not -- unlike tryLoad,
 * this never substitutes fabricated business data (assets, checkouts,
 * quotations, ...) for a real account, it only keeps app chrome (the
 * login page, the navbar brand) rendering sensibly if the very first
 * request of the session can't reach the backend, before demo/real-login
 * has even been decided.
 */
async function tryLoadPublicDefault<T>(loader: () => Promise<T>, fallback: T): Promise<T> {
  try {
    return await loader();
  } catch {
    return fallback;
  }
}

// ---------------------------------------------------------------------------
// auth -- POST /auth/login sets an httpOnly session cookie itself (see
// backend/api/auth_api.py's _set_session_cookie); there's no token for the
// frontend to store. GET /auth/me re-hydrates "who am I" on load / refresh.
// ---------------------------------------------------------------------------

export interface AuthUser {
  sub?: string;
  id?: number;
  name: string;
  email: string;
  username?: string | null;
  role: string;
  department?: string | null;
  department_role?: string | null;
}

export interface LoginResult {
  message?: string;
  mfa_required?: boolean;
  mfa_setup_required?: boolean;
  mfa_pending_token?: string;
  mfa_setup_token?: string;
  // Present only on an mfa_setup_required response (fresh enrollment, or
  // re-enrollment after a recovery code was used) -- shown exactly once,
  // never retrievable again. See README's "Two-factor authentication" section.
  totp_secret?: string;
  otpauth_uri?: string;
  // Present only on the response that finally confirms first-time 2FA
  // enrollment (POST /auth/mfa/setup/confirm) -- ten single-use backup
  // codes, also shown exactly once.
  recovery_codes?: string[];
}

export const auth = {
  // Backend's LoginRequest takes `identifier` (matched against either the
  // email or username column), not `email` -- see README.md's /auth/login
  // entry and schemas/auth_schema.py.
  login: (identifier: string, password: string) =>
    rawFetch<LoginResult>("/auth/login", { method: "POST", body: JSON.stringify({ identifier, password }) }),
  mfaVerify: (mfa_pending_token: string, code: string) =>
    rawFetch<LoginResult>("/auth/mfa/verify", { method: "POST", body: JSON.stringify({ mfa_pending_token, code }) }),
  // Completes first-time 2FA enrollment (or re-enrollment after a recovery
  // code retired the old secret): mfa_setup_token from the mfa_setup_required
  // response above, plus a live code from the authenticator app that was
  // just set up. This is the call that actually grants the session cookie.
  mfaSetupConfirm: (mfa_setup_token: string, code: string) =>
    rawFetch<LoginResult>("/auth/mfa/setup/confirm", { method: "POST", body: JSON.stringify({ mfa_setup_token, code }) }),
  logout: () => rawFetch<{ message: string }>("/auth/logout", { method: "POST" }),
  me: () => rawFetch<AuthUser>("/auth/me"),
  // POST /auth/forgot-password -- backend always responds with the same
  // generic message whether or not `identifier` matches an account (see
  // schemas/auth_schema.py's ForgotPasswordRequest docstring), so there's
  // nothing for the UI to branch on beyond a genuine network/server error.
  forgotPassword: (identifier: string) =>
    rawFetch<{ message?: string }>("/auth/forgot-password", { method: "POST", body: JSON.stringify({ identifier }) }),
  // POST /auth/reset-password -- completes the flow using the plaintext
  // `token` off the emailed link's ?reset_token= query param. Doesn't grant
  // a session; the person still signs in normally afterward with the new
  // password.
  resetPassword: (token: string, newPassword: string) =>
    rawFetch<{ message?: string }>("/auth/reset-password", { method: "POST", body: JSON.stringify({ token, new_password: newPassword }) }),
};

// ---------------------------------------------------------------------------
// mapping helpers -- translate backend/api/*.py response shapes into the
// types this UI was designed against (src/lib/types.ts).
// ---------------------------------------------------------------------------

function poolTag(assetId: number | null | undefined): string {
  return assetId != null ? `POOL-${String(assetId).padStart(4, "0")}` : "OUTSOURCED";
}

function assetStatus(available: number, total: number): AssetType["status"] {
  if (available <= 0) return "out";
  if (total > 0 && available / total <= 0.25) return "low";
  return "available";
}

interface RawAssetType {
  id: number;
  name: string;
  category: string | null;
  total_quantity: number;
  available_quantity: number;
  price: string | number | null;
}

function mapAsset(raw: RawAssetType): AssetType {
  const total = raw.total_quantity ?? 0;
  const available = raw.available_quantity ?? 0;
  return {
    id: raw.id,
    name: raw.name,
    category: raw.category ?? null,
    total_quantity: total,
    available_quantity: available,
    checked_out_quantity: Math.max(total - available, 0),
    price: raw.price != null ? Number(raw.price) : null,
    tag: poolTag(raw.id),
    status: assetStatus(available, total),
    // AssetType rows carry no updated_at column -- "now" is the closest
    // honest value available from this endpoint.
    updated_at: new Date().toISOString(),
  };
}

interface RawCheckout {
  checkout_id: number;
  asset_id: number | null;
  asset_name: string;
  assignee_name: string;
  assignee_type: string;
  quantity: number;
  outstanding: number;
  due_date: string | null; // "YYYY-MM-DD", null for an open-ended checkout
  is_overdue: boolean;
  is_due_soon: boolean;
  entity_id?: number | null;
  entity_type?: "user" | "outsider" | null;
}

function mapCheckout(raw: RawCheckout): Checkout {
  return {
    id: raw.checkout_id,
    asset_id: raw.asset_id ?? 0,
    asset_name: raw.asset_name,
    tag: poolTag(raw.asset_id),
    quantity: raw.outstanding ?? raw.quantity,
    checked_out_to: raw.assignee_name,
    checked_out_by: raw.assignee_type,
    due_at: raw.due_date ?? "",
    // GET /checkouts (this endpoint) returns due_date but not the
    // original checkout_date, so there's no real value to put here --
    // due_at is reused rather than inventing a timestamp (same tradeoff
    // mapCheckoutAlert below already makes for the overdue/due-soon feeds).
    checked_out_at: raw.due_date ?? "",
    status: raw.is_overdue ? "overdue" : "active",
    due_soon: raw.is_due_soon,
    entity_id: raw.entity_id ?? null,
    entity_type: raw.entity_type ?? null,
  };
}

interface RawCheckoutAlert {
  checkout_id: number;
  asset_id: number | null;
  asset_name: string;
  assignee_name: string;
  assignee_type: string;
  quantity: number;
  outstanding: number;
  due_date: string; // "YYYY-MM-DD"
  entity_id?: number | null;
  entity_type?: "user" | "outsider" | null;
}

function mapCheckoutAlert(raw: RawCheckoutAlert, status: Checkout["status"]): Checkout {
  return {
    id: raw.checkout_id,
    asset_id: raw.asset_id ?? 0,
    asset_name: raw.asset_name,
    tag: poolTag(raw.asset_id),
    quantity: raw.outstanding ?? raw.quantity,
    checked_out_to: raw.assignee_name,
    checked_out_by: raw.assignee_type,
    due_at: raw.due_date,
    // GET /checkouts/overdue and /checkouts/due-soon (the only checkout
    // feeds this app's role is guaranteed to reach) return due_date but
    // not the original checkout_date, so there's no real value to put
    // here -- due_at is reused rather than inventing a timestamp.
    checked_out_at: raw.due_date,
    status,
    entity_id: raw.entity_id ?? null,
    entity_type: raw.entity_type ?? null,
  };
}

interface RawExtensionRequest {
  id: number;
  checkout_id: number;
  asset_name: string;
  requested_by_label: string;
  requested_new_due_date: string;
  reason: string | null;
  status: "pending" | "approved" | "denied";
  entity_id?: number | null;
  entity_type?: "user" | "outsider" | null;
  assignee_name?: string;
}

function mapExtension(raw: RawExtensionRequest): ExtensionRequest {
  return {
    id: raw.id,
    checkout_id: raw.checkout_id,
    asset_name: raw.asset_name,
    requested_by: raw.requested_by_label,
    requested_until: raw.requested_new_due_date,
    reason: raw.reason ?? "",
    status: raw.status,
    entity_id: raw.entity_id ?? null,
    entity_type: raw.entity_type ?? null,
    assignee_name: raw.assignee_name ?? raw.requested_by_label,
  };
}

// ---------------------------------------------------------------------------
// loaders -- one real HTTP round trip (or a couple, combined) per page's
// need. Kept separate from the exported `api.*` methods so getStats()/
// getNotifications() can reuse getAssets()/getCheckouts()'s real loaders
// directly instead of re-fetching demo data underneath a live session.
// ---------------------------------------------------------------------------

async function loadAssets(): Promise<AssetType[]> {
  const data = await rawFetch<{ items: RawAssetType[] }>("/assets?limit=200");
  return (data.items ?? []).map(mapAsset);
}

async function loadOverdue(): Promise<Checkout[]> {
  const data = await rawFetch<{ items: RawCheckoutAlert[] }>("/checkouts/overdue?limit=100");
  return (data.items ?? []).map((r) => mapCheckoutAlert(r, "overdue"));
}

async function loadDueSoon(): Promise<Checkout[]> {
  const data = await rawFetch<{ items: RawCheckoutAlert[] }>("/checkouts/due-soon?limit=100");
  return (data.items ?? []).map((r) => mapCheckoutAlert(r, "active"));
}

export interface AlertFeed {
  items: Checkout[];
  total: number;
}

// Small, capped feeds purpose-built for the Notification Bell dropdown
// (js/components/overdue.js / due-soon.js's loadOverdueAlerts()/
// loadDueSoonAlerts()) -- unlike loadOverdue()/loadDueSoon() above (used
// for the Checkouts page's full table), the bell only ever shows a
// handful of rows grouped by person, but still needs the true `total`
// count for its badge. Privileged-role only on the backend
// (require_privileged_role); a Staff/Customer calling this just gets a
// harmless empty feed back via tryLoad's fallback.
export const alertsApi = {
  overdue: (limit = 5) =>
    tryLoad(async (): Promise<AlertFeed> => {
      const data = await rawFetch<{ items: RawCheckoutAlert[]; total: number }>(`/checkouts/overdue?limit=${limit}`);
      const items = data.items ?? [];
      return { items: items.map((r) => mapCheckoutAlert(r, "overdue")), total: data.total ?? items.length };
    }, { items: [], total: 0 }),
  dueSoon: (limit = 5) =>
    tryLoad(async (): Promise<AlertFeed> => {
      const data = await rawFetch<{ items: RawCheckoutAlert[]; total: number }>(`/checkouts/due-soon?limit=${limit}`);
      const items = data.items ?? [];
      return { items: items.map((r) => mapCheckoutAlert(r, "active")), total: data.total ?? items.length };
    }, { items: [], total: 0 }),
};

// GET /checkouts/overdue and /checkouts/due-soon are require_privileged_role
// on the backend (Super Admin/Admin/Manager -- see backend/deps.py). A
// Staff/Customer calling either just gets a 403. Every loader below that
// touches them therefore takes an explicit `privileged` flag from its
// caller (always `demo || isPrivileged(user?.role)`, mirroring
// NotificationBell.tsx) rather than trying-and-catching its way there --
// letting the 403 happen and fall through to tryLoad's fallback would
// silently hand a real, signed-in Staff/Customer session a screen full of
// fabricated demo data (mockCheckouts/mockStats/mockNotifications) the
// moment a live backend is linked, which is exactly the failure mode the
// demo dataset is meant to never cause.
async function loadMyItems(): Promise<MyItem[]> {
  try {
    const data = await rawFetch<{ assigned_items: MyItem[] }>("/users/me/items");
    return data.assigned_items ?? [];
  } catch {
    return [];
  }
}

// Mutable module-level cache of the LAST-SEEN settings.DUE_SOON_REMINDER_DAYS
// value (.env, echoed back by GET /checkouts -- see backend/services/
// checkout_service.py's list_active_checkouts()). Set every time
// loadCheckouts() below actually hits the real endpoint; read by the
// Checkouts page to label its "Due soon" column with the true configured
// window instead of a hardcoded guess. Starts at the backend's own
// documented default (config.py's DUE_SOON_REMINDER_DAYS) so the column
// still reads sensibly before the first successful load.
let lastKnownDueSoonReminderDays = 2;
export function getDueSoonReminderDays(): number {
  return lastKnownDueSoonReminderDays;
}

async function loadCheckouts(privileged: boolean): Promise<Checkout[]> {
  // GET /checkouts (root) is the real, full "who has what" table --
  // every ACTIVE checkout org-wide, not just the ones that happen to be
  // overdue or landing within the due-soon reminder window. It used to
  // fall back to combining the overdue + due-soon alert feeds instead
  // (the closest analogue available before this endpoint existed), which
  // silently hid any healthy, non-imminent checkout from the Checkouts
  // page's "All" tab -- a freshly dispatched item with a due date weeks
  // out would never appear anywhere. Not privileged? There's no
  // system-wide table to show at all -- the Dashboard falls back to that
  // person's own items instead (see Dashboard.tsx), same split as
  // legacy's admin.html/manager.html (system-wide) vs staff.html/
  // customer.html (personal only).
  if (!privileged) return [];
  const data = await rawFetch<{ items: RawCheckout[]; total: number; due_soon_reminder_days: number }>("/checkouts?limit=500");
  if (typeof data.due_soon_reminder_days === "number") {
    lastKnownDueSoonReminderDays = data.due_soon_reminder_days;
  }
  return (data.items ?? []).map(mapCheckout);
}

async function loadExtensionRequests(): Promise<ExtensionRequest[]> {
  const data = await rawFetch<{ items: RawExtensionRequest[] }>("/checkouts/extension-requests?status=pending&limit=100");
  return (data.items ?? []).map(mapExtension);
}

async function loadNotifications(privileged: boolean): Promise<NotificationItem[]> {
  // There's no in-app notification-feed endpoint on the backend today
  // (api/notifications_api.py is only the digest-email recipient
  // settings) -- same as the legacy frontend's notification bell
  // (js/components/notifications.js), this is synthesized client-side.
  // WHO SEES WHAT mirrors NotificationBell.tsx/legacy notifications.js:
  // the review-facing org-wide feeds (overdue/due-soon/pending extension
  // requests) only for a privileged role; everyone (including Staff/
  // Customer) gets their own personal item alerts from /users/me/items.
  const personalItems = await loadMyItems();
  const personal: NotificationItem[] = [
    ...personalItems.filter((i) => i.overdue).map((i) => ({
      id: 4_000_000 + i.checkout_id,
      title: `${i.asset_name} is overdue`,
      body: `You still have ${i.quantity} unit(s) checked out, due ${formatDate(i.due_date)}.`,
      kind: "overdue" as const,
      created_at: i.due_date,
      read: false,
    })),
    ...personalItems.filter((i) => i.due_soon && !i.overdue).map((i) => ({
      id: 5_000_000 + i.checkout_id,
      title: `${i.asset_name} is due soon`,
      body: `You have ${i.quantity} unit(s) due back ${formatDate(i.due_date)}.`,
      kind: "system" as const,
      created_at: i.due_date,
      read: true,
    })),
  ];

  if (!privileged) {
    return personal.sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime());
  }

  const [overdue, dueSoon, extensions] = await Promise.all([
    loadOverdue(),
    loadDueSoon(),
    loadExtensionRequests(),
  ]);

  const items: NotificationItem[] = [
    ...personal,
    ...overdue.map((c) => ({
      id: 1_000_000 + c.id,
      title: `${c.asset_name} is overdue`,
      body: `${c.checked_out_to} still has ${c.quantity} unit(s) checked out, due ${formatDate(c.due_at)}.`,
      kind: "overdue" as const,
      created_at: c.due_at,
      read: false,
    })),
    ...dueSoon.map((c) => ({
      id: 2_000_000 + c.id,
      title: `${c.asset_name} due soon`,
      body: `${c.checked_out_to} needs to return ${c.quantity} unit(s) by ${formatDate(c.due_at)}.`,
      kind: "system" as const,
      created_at: c.due_at,
      read: true,
    })),
    ...extensions.map((e) => ({
      id: 3_000_000 + e.id,
      title: `Extension requested for ${e.asset_name}`,
      body: `${e.requested_by} asked to push the due date to ${formatDate(e.requested_until)}.`,
      kind: "extension" as const,
      created_at: e.requested_until,
      read: e.status !== "pending",
    })),
  ];

  return items.sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime());
}

async function loadStats(privileged: boolean): Promise<DashboardStats> {
  // total_assets/available/low_stock/categories all come from GET /assets,
  // which is open to any signed-in role -- only the overdue/due-soon
  // counts need the privileged-only feeds, so those two are swapped for
  // that person's OWN overdue/due-soon count (from /users/me/items, open
  // to everyone) when the signed-in role isn't privileged, rather than
  // ever attempting the org-wide endpoints and risking a 403 -> mock
  // fallback for a real Staff/Customer session.
  const [assets, overdueCount, dueSoonCount, activity] = await Promise.all([
    loadAssets(),
    privileged ? loadOverdue().then((r) => r.length) : loadMyItems().then((items) => items.filter((i) => i.overdue).length),
    privileged ? loadDueSoon().then((r) => r.length) : loadMyItems().then((items) => items.filter((i) => i.due_soon && !i.overdue).length),
    rawFetch<{ date: string; checkouts: number; returns: number }[]>("/assets/activity").catch(() => []),
  ]);

  const totalAssets = assets.reduce((sum, a) => sum + a.total_quantity, 0);
  const available = assets.reduce((sum, a) => sum + a.available_quantity, 0);
  const lowStock = assets.filter((a) => a.status === "low" || a.status === "out").length;

  const byCategory = new Map<string, number>();
  for (const a of assets) {
    const key = a.category ?? "Uncategorized";
    byCategory.set(key, (byCategory.get(key) ?? 0) + a.total_quantity);
  }

  return {
    total_assets: totalAssets,
    available,
    checked_out: Math.max(totalAssets - available, 0),
    overdue: overdueCount,
    due_soon: dueSoonCount,
    low_stock: lowStock,
    categories: Array.from(byCategory, ([name, count]) => ({ name, count })),
    // GET /assets/activity -- daily checkout/return counts, same shape as
    // this field always expected. Falls back to an empty series (rather
    // than fabricated data) if the request fails for any reason.
    activity,
  };
}

export const api = {
  isLive: () => backendReachable === true,
  getAssets: () => tryLoad(loadAssets, mockAssets),
  // `privileged` mirrors `demo || isPrivileged(user?.role)` at every call
  // site (Dashboard.tsx/Layout.tsx/Notifications.tsx) -- see the loaders
  // above for why this can't just try the privileged endpoints and let
  // tryLoad's fallback catch the 403 the way getAssets() above safely can.
  getCheckouts: (privileged: boolean) => tryLoad(() => loadCheckouts(privileged), mockCheckouts),
  getExtensionRequests: () => tryLoad(loadExtensionRequests, mockExtensions),
  getNotifications: (privileged: boolean) => tryLoad(() => loadNotifications(privileged), mockNotifications),
  getStats: (privileged: boolean) => tryLoad(() => loadStats(privileged), mockStats),
};

// ---------------------------------------------------------------------------
// admin: System Backups -- every /backup/* route is gated on the backend by
// deps.require_true_super_admin (root Super Admin only; a regular `admin`
// account, though otherwise equivalent, is deliberately excluded here --
// see backend/deps.py). Callers (pages/Admin.tsx) additionally hide the
// panel client-side, same belt-and-suspenders spirit as the legacy
// frontend's isTrueSuperAdmin() check in js/components/backups.js.
// ---------------------------------------------------------------------------

export const backupApi = {
  status: () => tryLoad(() => rawFetch<BackupStatus>("/backup/status"), mockBackupStatus),
  list: () => tryLoad(() => rawFetch<BackupEntry[]>("/backup/list"), mockBackups),
  create: () => rawFetch<{ message?: string }>("/backup/create", { method: "POST" }),
  remove: (filename: string) => rawFetch<void>(`/backup/${encodeURIComponent(filename)}`, { method: "DELETE" }),
  restoreLocal: (filename: string) => rawFetch<RestoreResult>(`/backup/restore/${encodeURIComponent(filename)}`, { method: "POST" }),
  restoreUpload: (file: File) => {
    const formData = new FormData();
    formData.append("file", file);
    return rawFetchMultipart<RestoreResult>("/backup/restore-upload", formData);
  },
  // Not a JSON round trip -- the browser needs a plain navigable/`<a>`-able
  // URL (the httpOnly session cookie rides along automatically since this
  // stays same-origin), same approach as the legacy frontend's downloadBackup().
  downloadUrl: (filename: string) => `${API_BASE}/backup/download/${encodeURIComponent(filename)}`,
};

// ---------------------------------------------------------------------------
// admin: Daily Digest Recipients -- GET/PUT /settings/digest-recipients,
// Super Admin/Admin only (backend/api/notifications_api.py). Edited as a
// full list: every add/remove re-PUTs the whole array.
// ---------------------------------------------------------------------------

export const digestApi = {
  list: () => tryLoad(async () => (await rawFetch<{ emails: string[] }>("/settings/digest-recipients")).emails ?? [], mockDigestRecipients),
  set: (emails: string[]) => rawFetch<{ emails: string[] }>("/settings/digest-recipients", { method: "PUT", body: JSON.stringify({ emails }) }),
};

// ---------------------------------------------------------------------------
// admin: Inventory Import -- POST /assets/import, Super Admin or Admin
// (deps.require_super_admin, the broader "full admin" gate -- unlike
// backups, this one IS available to a plain `admin` account).
// ---------------------------------------------------------------------------

export const importApi = {
  importCsv: (file: File) => {
    const formData = new FormData();
    formData.append("file", file);
    return rawFetchMultipart<ImportResult>("/assets/import", formData);
  },
};

// ---------------------------------------------------------------------------
// self-service: My Items (GET /users/me/items -- any authenticated user)
// and Extension Requests (request/review/decide -- backend/api/
// checkouts_api.py). Review/decide is require_privileged_role
// (Super Admin/Admin/Manager); requesting is open to whoever holds the
// checkout.
// ---------------------------------------------------------------------------

export const myItemsApi = {
  // `limit`/`offset` are optional -- the Notification Bell and Dashboard
  // call this with none of these (getting the backend's generous default
  // page, effectively "everything" for one person's custody ledger), while
  // the My Items table (see pages/MyItems.tsx) passes them explicitly for
  // TRUE server-side pagination, same pattern as assetsApi.list/usersApi.list.
  list: (limit?: number, offset?: number) =>
    rawFetch<{ name: string; department_role?: string | null; assigned_items: MyItem[]; total: number; limit: number; offset: number }>(
      `/users/me/items${qs({ limit, offset })}`
    ),
  // Not a JSON round trip -- GET /users/me/items/export streams the file
  // directly (same pattern as assetsApi.exportUrl below). Ported from the
  // legacy frontend's components/exports.js's exportMyItems().
  exportUrl: (format: "csv" | "pdf") => `${API_BASE}/users/me/items/export${qs({ format })}`,
};

export const extensionsApi = {
  request: (checkoutId: number, newDueDate: string, reason: string) =>
    rawFetch<{ message?: string }>(`/checkouts/${checkoutId}/extension-requests`, {
      method: "POST",
      body: JSON.stringify({ new_due_date: newDueDate, reason: reason || null }),
    }),
  listPending: () => tryLoad(loadExtensionRequests, []),
  // TRUE server-side pagination for the Checkouts page's "Extension
  // requests" side panel -- GET /checkouts/extension-requests already
  // accepts limit/offset server-side (see
  // backend/services/extension_service.py's list_extension_requests()),
  // this just pages against it directly instead of fetching up to 100
  // pending requests in one shot and slicing that in-memory list.
  list: (limit: number, offset: number) =>
    tryLoad(
      async () => {
        const data = await rawFetch<{ items: RawExtensionRequest[]; total: number }>(
          `/checkouts/extension-requests${qs({ status: "pending", limit, offset })}`
        );
        return { items: (data.items ?? []).map(mapExtension), total: data.total ?? 0 };
      },
      { items: mockExtensions, total: mockExtensions.length }
    ),
  decide: (requestId: number, approve: boolean, note: string | null) =>
    rawFetch<{ message?: string }>(`/checkouts/extension-requests/${requestId}/decision`, {
      method: "POST",
      body: JSON.stringify({ approve, note }),
    }),
  // Self-service alert feed: the CALLER's own recently approved/denied
  // extension requests -- open to any logged-in account. Powers the
  // Notification Bell's "My Extension Decisions" section. See
  // backend/services/extension_service.py's list_my_recent_extension_decisions().
  myDecisions: (limit = 10) =>
    tryLoad(async () => {
      const data = await rawFetch<{ items: MyExtensionDecision[]; total: number }>(`/checkouts/my-extension-decisions?limit=${limit}`);
      return data.items ?? [];
    }, [] as MyExtensionDecision[]),
};

// ---------------------------------------------------------------------------
// self-service: My Profile (backend/api/auth_api.py) -- available to every
// role. Identity (name/email/username) rotation is Super Admin only on
// the legacy frontend; mirrored the same way here (see pages/Profile.tsx).
// ---------------------------------------------------------------------------

export const profileApi = {
  get: () => rawFetch<ProfileDetail>("/auth/me"),
  updatePassword: (userId: number, currentPassword: string, newPassword: string) =>
    rawFetch<{ message?: string }>("/auth/update-password", {
      method: "POST",
      body: JSON.stringify({ user_id: userId, current_password: currentPassword, new_password: newPassword }),
    }),
  updateIdentity: (name: string, email: string, username: string, currentPassword: string, phoneNumber?: string, company?: string) =>
    rawFetch<ProfileDetail & { message?: string }>("/auth/me", {
      method: "PATCH",
      body: JSON.stringify({
        name, email, username, current_password: currentPassword,
        phone_number: phoneNumber, company,
      }),
    }),
  regenerateRecoveryCodes: (password: string) =>
    rawFetch<{ recovery_codes: string[] }>("/auth/mfa/recovery-codes/regenerate", {
      method: "POST",
      body: JSON.stringify({ password }),
    }),
};

// ---------------------------------------------------------------------------
// admin: User Directory (backend/api/users_api.py). List/search is
// require_privileged_role (Super Admin/Admin/Manager); delete/restore/
// purge/reset-password are require_super_admin only.
// ---------------------------------------------------------------------------

export interface DirectoryPage<T> { items: T[]; total: number; limit: number; offset: number }

function qs(params: Record<string, string | number | undefined>): string {
  const parts = Object.entries(params).filter(([, v]) => v !== undefined && v !== "");
  return parts.length ? `?${parts.map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`).join("&")}` : "";
}

export const usersApi = {
  list: (limit: number, offset: number, search: string) => rawFetch<DirectoryPage<UserRow>>(`/users${qs({ limit, offset, search })}`),
  create: (req: { name: string; email: string; phone_number?: string; role: string; password: string; department?: string; department_role?: string }) =>
    rawFetch<UserRow>("/users", { method: "POST", body: JSON.stringify(req) }),
  update: (id: number, req: Partial<{ name: string; username: string; email: string; phone_number: string; company: string }>) =>
    rawFetch<UserRow>(`/users/${id}`, { method: "PATCH", body: JSON.stringify(req) }),
  resetPassword: (id: number, newPassword: string, adminPassword: string) =>
    rawFetch<{ message?: string }>(`/users/${id}/reset-password`, { method: "POST", body: JSON.stringify({ new_password: newPassword, admin_password: adminPassword }) }),
  remove: (id: number) => rawFetch<void>(`/users/${id}`, { method: "DELETE" }),
  restore: (id: number) => rawFetch<{ message?: string }>(`/users/${id}/restore`, { method: "POST" }),
  purge: (id: number) => rawFetch<{ message?: string }>(`/users/${id}/purge`, { method: "POST" }),
  listDeleted: (limit: number, offset: number, search: string) => rawFetch<DirectoryPage<DeletedUserRow>>(`/users/deleted${qs({ limit, offset, search })}`),
  convertToOutsider: (id: number, req: Partial<{ email: string; phone_number: string; company: string }>) =>
    rawFetch<OutsiderRow>(`/users/${id}/convert-to-outsider`, { method: "POST", body: JSON.stringify(req) }),
  items: (id: number) => rawFetch<{ name: string; assigned_items: CustodyItem[] }>(`/users/${id}/items`),
  // Direct-download links (not JSON round trips), mirrored from the legacy
  // frontend's components/exports.js -- exportCustodyItems() for one
  // person's ledger, exportAllUsers() for the whole directory.
  itemsExportUrl: (id: number, format: "csv" | "pdf") => `${API_BASE}/users/${id}/items/export${qs({ format })}`,
  exportUrl: (format: "csv" | "pdf") => `${API_BASE}/users/export${qs({ format })}`,
};

// ---------------------------------------------------------------------------
// admin: Ad-Hoc (Unlinked) Directory (backend/api/outsiders_api.py) --
// external individuals dispatched equipment without a login. Same
// require_privileged_role gate throughout.
// ---------------------------------------------------------------------------

export const outsidersApi = {
  list: (limit: number, offset: number, search: string) => rawFetch<DirectoryPage<OutsiderRow>>(`/outsiders${qs({ limit, offset, search })}`),
  update: (id: number, req: Partial<{ name: string; email: string; phone_number: string; company: string }>) =>
    rawFetch<OutsiderRow>(`/outsiders/${id}`, { method: "PATCH", body: JSON.stringify(req) }),
  remove: (id: number) => rawFetch<void>(`/outsiders/${id}`, { method: "DELETE" }),
  convertToUser: (id: number, req: { email: string; phone_number?: string; password: string; role: string; department?: string; department_role?: string }) =>
    rawFetch<UserRow>(`/outsiders/${id}/convert-to-user`, { method: "POST", body: JSON.stringify(req) }),
  items: (id: number) => rawFetch<{ name: string; assigned_items: CustodyItem[] }>(`/outsiders/${id}/items`),
  // Direct-download links, mirrored from the legacy frontend's
  // components/exports.js -- exportCustodyItems() for one profile's
  // ledger, exportAllOutsiders() for the whole Ad-Hoc Directory.
  itemsExportUrl: (id: number, format: "csv" | "pdf") => `${API_BASE}/outsiders/${id}/items/export${qs({ format })}`,
  exportUrl: (format: "csv" | "pdf") => `${API_BASE}/outsiders/export${qs({ format })}`,
};

// ---------------------------------------------------------------------------
// admin: Audit Trail (backend/api/audit_api.py) -- require_privileged_role.
// Export runs async on a Celery worker: start -> poll status -> download.
// ---------------------------------------------------------------------------

export const auditApi = {
  list: (limit: number, offset: number) => rawFetch<DirectoryPage<AuditLogEntry>>(`/audit-logs${qs({ limit, offset })}`),
  startExport: (format: "csv" | "pdf", startDate?: string, endDate?: string) =>
    rawFetch<{ task_id: string }>(`/audit-logs/export${qs({ format, start_date: startDate, end_date: endDate })}`, { method: "POST" }),
  exportStatus: (taskId: string) => rawFetch<{ state: string; ready: boolean; error?: string }>(`/audit-logs/export/${taskId}/status`),
  downloadUrl: (taskId: string) => `${API_BASE}/audit-logs/export/${taskId}/download`,
};

// ---------------------------------------------------------------------------
// admin: Reporting / Analytics dashboard (backend/api/reports_api.py) --
// require_privileged_role (Super Admin/Admin/Manager). One combined GET
// /reports/dashboard call per page load/filter change -- see
// backend/services/reports_service.py's get_dashboard() for how each
// section is derived. tryLoad-wrapped like the other org-wide, privileged
// views (getCheckouts/auditApi) so "Demo browsing" still renders something
// explorable (see lib/mock.ts's mockReportsDashboard) while a real signed-in
// session that hits a genuine error surfaces it instead of silently
// swapping in fabricated numbers.
// ---------------------------------------------------------------------------

export const reportsApi = {
  dashboard: (startDate?: string, endDate?: string, category?: string) =>
    tryLoad(
      () => rawFetch<ReportsDashboard>(`/reports/dashboard${qs({ start_date: startDate, end_date: endDate, category })}`),
      mockReportsDashboard
    ),
};

// ---------------------------------------------------------------------------
// checkouts: return processing, shared by the Custody Ledger drawer for
// both the User Directory and the Ad-Hoc Directory (require_privileged_role).
// ---------------------------------------------------------------------------

export const checkoutsApi = {
  // TRUE server-side pagination for the Checkouts page's table (all four
  // tabs) -- same pattern as usersApi.list/assetsApi.list/myItemsApi.list:
  // `limit`/`offset` page the query itself, and `filter` narrows it to one
  // tab's subset (see backend/services/checkout_service.py's
  // list_active_checkouts() `status_filter` param) rather than fetching
  // every active checkout and slicing it client-side. The is_overdue/
  // is_due_soon math (and due_soon_reminder_days) is computed fresh
  // against "now" on every call, same as the unfiltered load, so a
  // checkout's tab membership can never go stale between page turns.
  list: (limit: number, offset: number, filter?: "overdue" | "due_soon" | "active") =>
    rawFetch<{ items: RawCheckout[]; total: number; due_soon_reminder_days: number }>(
      `/checkouts${qs({ limit, offset, filter })}`
    ).then((data) => {
      if (typeof data.due_soon_reminder_days === "number") {
        lastKnownDueSoonReminderDays = data.due_soon_reminder_days;
      }
      return { items: (data.items ?? []).map(mapCheckout), total: data.total ?? 0 };
    }),
  returnItem: (checkoutId: number, quantity: number) => rawFetch<{ message?: string }>(`/checkouts/${checkoutId}/return`, { method: "POST", body: JSON.stringify({ quantity }) }),
  // Direct grant, no request/decision round trip -- the Custody Ledger
  // drawer's "Extend" button (require_privileged_role). See
  // backend/services/extension_service.py's extend_checkout_directly().
  extend: (checkoutId: number, newDueDate: string, reason: string | null) =>
    rawFetch<{ message?: string; due_date?: string }>(`/checkouts/${checkoutId}/extend`, {
      method: "POST",
      body: JSON.stringify({ new_due_date: newDueDate, reason: reason || null }),
    }),
  // Applies one new due date to many active checkouts at once -- the
  // Custody Ledger drawer's "Bulk Extend Selected" action, reusing the
  // same checkbox selection used for bulk returns. See
  // backend/services/extension_service.py's extend_checkouts_bulk().
  bulkExtend: (checkoutIds: number[], newDueDate: string, reason: string | null) =>
    rawFetch<BulkExtendResult>(`/checkouts/bulk-extend`, {
      method: "POST",
      body: JSON.stringify({ checkout_ids: checkoutIds, new_due_date: newDueDate, reason: reason || null }),
    }),
};

// ---------------------------------------------------------------------------
// Asset Inventory core -- ported from the legacy frontend's
// js/components/assets.js. Backed by backend/api/assets_api.py +
// backend/services/asset_service.py. List/details/categories/export are
// open to any authenticated user (get_current_user); everything that
// mutates a pool (create/rename/recategorize/reprice/capacity/delete/
// restore/purge/exception/recall) is require_super_admin; dispatch
// (checkout_advanced) is require_privileged_role (Super Admin/Admin/Manager).
// ---------------------------------------------------------------------------

export interface AssetTypeCreateRequest {
  name: string;
  total_quantity: number;
  category?: string | null;
  price?: number | null;
}

export interface AdvancedCheckoutPayload {
  assignee_type: "user" | "outsider";
  quantity: number;
  due_date?: string | null;
  user_id?: number;
  outsider_id?: number;
  outsider_name?: string;
  outsider_email?: string | null;
  outsider_phone?: string | null;
  outsider_company?: string | null;
}

export const assetsApi = {
  // tryLoad-wrapped (unlike the rest of this module) so the main Inventory
  // page -- reachable by every role, including someone just browsing demo
  // data with no backend session -- keeps working with client-paginated
  // mock data, same spirit as api.getAssets() above.
  list: (limit: number, offset: number, search: string, category?: string): Promise<DirectoryPage<AssetType>> =>
    tryLoad(async () => {
      const data = await rawFetch<{ items: RawAssetType[]; total: number; limit: number; offset: number }>(`/assets${qs({ limit, offset, search, category })}`);
      return { items: (data.items ?? []).map(mapAsset), total: data.total, limit: data.limit, offset: data.offset };
    }, (() => {
      // Same filter behavior as backend/services/asset_service.py's
      // list_assets() -- "All"/"" means no category filter, "Uncategorized"
      // matches a null category -- so demo mode (no backend session) stays
      // consistent with a real one instead of the two disagreeing.
      const cat = (category ?? "").trim();
      let filtered = search.trim()
        ? mockAssets.filter((a) => a.name.toLowerCase().includes(search.trim().toLowerCase()))
        : mockAssets;
      if (cat && cat.toLowerCase() !== "all") {
        filtered = filtered.filter((a) => (a.category ?? "Uncategorized").toLowerCase() === cat.toLowerCase());
      }
      return { items: filtered.slice(offset, offset + limit), total: filtered.length, limit, offset };
    })()),
  listDeleted: (limit: number, offset: number, search: string) => rawFetch<DirectoryPage<DeletedAssetRow>>(`/assets/deleted${qs({ limit, offset, search })}`),
  categories: () => rawFetch<{ categories: string[] }>("/assets/categories"),
  activity: (days = 14) => rawFetch<{ date: string; checkouts: number; returns: number }[]>(`/assets/activity${qs({ days })}`),
  details: (assetId: number) => rawFetch<AssetDetails>(`/assets/${assetId}/details`),
  create: (payload: AssetTypeCreateRequest) => rawFetch<{ message?: string; id: number }>("/assets", { method: "POST", body: JSON.stringify(payload) }),
  updateQuantity: (assetId: number, newTotal: number) => rawFetch<{ message?: string }>(`/assets/${assetId}/quantity`, { method: "PUT", body: JSON.stringify({ new_total: newTotal }) }),
  updateName: (assetId: number, name: string) => rawFetch<{ message?: string }>(`/assets/${assetId}/name`, { method: "PUT", body: JSON.stringify({ name }) }),
  updateCategory: (assetId: number, category: string | null) => rawFetch<{ message?: string }>(`/assets/${assetId}/category`, { method: "PUT", body: JSON.stringify({ category }) }),
  updatePrice: (assetId: number, price: number | null) => rawFetch<{ message?: string }>(`/assets/${assetId}/price`, { method: "PUT", body: JSON.stringify({ price }) }),
  remove: (assetId: number) => rawFetch<{ message?: string }>(`/assets/${assetId}`, { method: "DELETE" }),
  restore: (assetId: number) => rawFetch<{ message?: string }>(`/assets/${assetId}/restore`, { method: "POST" }),
  purge: (assetId: number) => rawFetch<{ message?: string }>(`/assets/${assetId}/purge`, { method: "POST" }),
  flagException: (assetId: number, payload: { serial_number: string; status_label: string; notes?: string | null }) =>
    rawFetch<{ message?: string }>(`/assets/${assetId}/exception`, { method: "POST", body: JSON.stringify(payload) }),
  recallException: (assetId: number, exceptionId: number) =>
    rawFetch<{ message?: string }>(`/assets/${assetId}/exception/${exceptionId}/recall`, { method: "POST" }),
  checkoutAdvanced: (assetId: number, payload: AdvancedCheckoutPayload) =>
    rawFetch<{ message?: string }>(`/assets/${assetId}/checkout_advanced`, { method: "POST", body: JSON.stringify(payload) }),
  // Roster helpers powering the Dispatch drawer's "Assign To" dropdowns --
  // separate, unfiltered fetches (mirrors users.js/outsiders.js's
  // loadUsers()/loadOutsiders() populating #staffSelect/#customerSelect/
  // #adhocExistingSelect), not the paginated/search-narrowed directory
  // slices those pages themselves show.
  staffRoster: async (): Promise<RosterUser[]> => {
    const data = await rawFetch<{ items: RosterUser[] }>("/users?limit=1000");
    return (data.items ?? []).filter((u) => u.role !== "customer");
  },
  customerRoster: async (): Promise<RosterUser[]> => {
    const data = await rawFetch<{ items: RosterUser[] }>("/users?limit=1000");
    return (data.items ?? []).filter((u) => u.role === "customer");
  },
  outsiderRoster: () => rawFetch<{ items: OutsiderRow[] }>("/outsiders?limit=1000").then((d) => d.items ?? []),
  // Not a JSON round trip -- GET /assets/export streams the file directly
  // (see backend's Response(..., headers={"Content-Disposition": ...})),
  // same same-origin-cookie approach as backupApi.downloadUrl above.
  exportUrl: (format: "csv" | "pdf", category?: string) => `${API_BASE}/assets/export${qs({ format, category })}`,
};

// ---------------------------------------------------------------------------
// Quotation feature -- ported from the legacy frontend's
// js/components/quotation.js. Backed by backend/api/quotations_api.py +
// backend/services/quotation_service.py; see that module's own docstring
// for the two-halves shape (self-service cart/history vs Admin/Manager
// Quotes tab) this mirrors.
// ---------------------------------------------------------------------------

export const quotationsApi = {
  // Deliberately NOT tryLoad -- see tryLoadPublicDefault's own docstring
  // just above: this is a safe UI default (currency/site name), never a
  // fabricated demo dataset, and it needs to fall back even for a real
  // account since it's fetched before login/demo mode is known.
  publicConfig: () => tryLoadPublicDefault(() => rawFetch<PublicConfig>("/config/public"), { currency_code: "NGN", site_name: "Ledger", show_stock_to_staff_customer: true }),
  // Omitting limit/offset/search returns the WHOLE active catalog in one
  // response (backend default -- see services/quotation_service.py's
  // list_catalog()), which is what every full-catalog consumer still
  // wants (the Admin/Manager Quote Detail drawer's "Add another asset"
  // typeahead searches this list entirely client-side).
  catalog: () => tryLoad(async () => (await rawFetch<{ items: CatalogAsset[]; show_stock: boolean }>("/assets/catalog")).items, mockCatalog),
  // True server-side paged + searched catalog page -- same
  // limit/offset/search -> {items,total,limit,offset} contract as
  // assetsApi.list/usersApi.list/etc (DirectoryPage<T>). Used by the
  // Quotations page's own browsable "Asset Catalog" table.
  catalogPage: (limit: number, offset: number, search: string): Promise<DirectoryPage<CatalogAsset>> =>
    tryLoad(async () => {
      const data = await rawFetch<{ items: CatalogAsset[]; total: number; limit: number; offset: number; show_stock: boolean }>(`/assets/catalog${qs({ limit, offset, search })}`);
      return { items: data.items ?? [], total: data.total, limit: data.limit, offset: data.offset };
    }, (() => {
      const filtered = search.trim()
        ? mockCatalog.filter((a) => a.name.toLowerCase().includes(search.trim().toLowerCase()) || (a.category ?? "").toLowerCase().includes(search.trim().toLowerCase()))
        : mockCatalog;
      return { items: filtered.slice(offset, offset + limit), total: filtered.length, limit, offset };
    })()),

  // ---- self-service: "My Order" (draft cart) ----
  myCart: () => tryLoad(() => rawFetch<QuotationCartOrDetail>("/quotations/me"), mockQuotationCart),
  addToCart: (assetId: number, quantity: number, startDate: string, dueDate: string) =>
    rawFetch<QuotationCartOrDetail>("/quotations/items", {
      method: "POST",
      body: JSON.stringify({ asset_id: assetId, quantity, start_date: startDate, due_date: dueDate }),
    }),
  updateCartItem: (itemId: number, quantity: number) =>
    rawFetch<QuotationCartOrDetail>(`/quotations/items/${itemId}`, { method: "PUT", body: JSON.stringify({ quantity }) }),
  removeCartItem: (itemId: number) => rawFetch<QuotationCartOrDetail>(`/quotations/items/${itemId}`, { method: "DELETE" }),
  submitCart: () => rawFetch<QuotationCartOrDetail>("/quotations/submit", { method: "POST" }),

  // ---- self-service: "My Quotes" (submitted history + own detail) ----
  myHistory: () => tryLoad(async () => (await rawFetch<{ items: QuotationListRow[] }>("/quotations/me/history")).items, []),
  myQuoteDetail: (quotationId: number) => rawFetch<QuotationCartOrDetail>(`/quotations/me/${quotationId}`),
  updateMyQuoteItem: (quotationId: number, itemId: number, quantity: number) =>
    rawFetch<QuotationCartOrDetail>(`/quotations/me/${quotationId}/items/${itemId}`, { method: "PUT", body: JSON.stringify({ quantity }) }),
  removeMyQuoteItem: (quotationId: number, itemId: number) =>
    rawFetch<QuotationCartOrDetail>(`/quotations/me/${quotationId}/items/${itemId}`, { method: "DELETE" }),

  // ---- self-service: in-app "Quotation updates" notifications ----
  // (assigned/updated alerts -- see lib/types.ts's QuotationNotification
  // and backend/services/quotation_service.py's
  // _notify_quotation_recipient()). Fails soft to an empty list, same as
  // every other alert feed the Notification Bell polls, so a transient
  // error here never breaks the bell's overall unread count.
  myNotifications: () =>
    tryLoad(async () => (await rawFetch<{ items: QuotationNotification[] }>("/quotations/me/notifications")).items, []),
  markNotificationsRead: (notificationIds: number[]) =>
    rawFetch<{ updated: number }>("/quotations/me/notifications/read", {
      method: "POST",
      body: JSON.stringify({ notification_ids: notificationIds }),
    }),
  addMyQuoteItem: (quotationId: number, assetId: number, quantity: number, startDate: string, dueDate: string) =>
    rawFetch<QuotationCartOrDetail>(`/quotations/me/${quotationId}/items`, {
      method: "POST",
      body: JSON.stringify({ asset_id: assetId, quantity, start_date: startDate, due_date: dueDate }),
    }),

  // ---- Admin/Manager: the "Quotes" tab (require_privileged_role) ----
  list: (limit: number, offset: number, search: string) =>
    rawFetch<DirectoryPage<QuotationListRow>>(`/quotations${qs({ limit, offset, search })}`),
  detail: (quotationId: number) => rawFetch<QuotationCartOrDetail & { id: number }>(`/quotations/${quotationId}`),
  create: (payload: Record<string, unknown>) => rawFetch<QuotationCartOrDetail & { id: number }>("/quotations", { method: "POST", body: JSON.stringify(payload) }),
  remove: (quotationId: number) => rawFetch<void>(`/quotations/${quotationId}`, { method: "DELETE" }),
  approve: (quotationId: number) => rawFetch<QuotationCartOrDetail>(`/quotations/${quotationId}/approve`, { method: "POST" }),
  saveNotes: (quotationId: number, notes: string) =>
    rawFetch<QuotationCartOrDetail>(`/quotations/${quotationId}`, { method: "PUT", body: JSON.stringify({ notes: notes || null }) }),
  saveDiscount: (quotationId: number, discountPercent: number) =>
    rawFetch<QuotationCartOrDetail>(`/quotations/${quotationId}/discount`, { method: "PUT", body: JSON.stringify({ discount_percent: discountPercent }) }),
  addItem: (quotationId: number, assetId: number, quantity: number, startDate: string, dueDate: string) =>
    rawFetch<QuotationCartOrDetail>(`/quotations/${quotationId}/items`, {
      method: "POST",
      body: JSON.stringify({ asset_id: assetId, quantity, start_date: startDate, due_date: dueDate }),
    }),
  updateItem: (quotationId: number, itemId: number, quantity: number) =>
    rawFetch<QuotationCartOrDetail>(`/quotations/${quotationId}/items/${itemId}`, { method: "PUT", body: JSON.stringify({ quantity }) }),
  removeItem: (quotationId: number, itemId: number) => rawFetch<QuotationCartOrDetail>(`/quotations/${quotationId}/items/${itemId}`, { method: "DELETE" }),
  assign: (quotationId: number, payload: Record<string, unknown>) =>
    rawFetch<QuotationCartOrDetail>(`/quotations/${quotationId}/assign`, { method: "POST", body: JSON.stringify(payload) }),

  // ---- Admin/Manager-only: "not currently in inventory" lines (Manager/
  // Admin-added, requester can see but not edit/remove) ----
  addOutsourcedItem: (quotationId: number, payload: QuotationOutsourcedItemCreate) =>
    rawFetch<QuotationCartOrDetail>(`/quotations/${quotationId}/outsourced-items`, { method: "POST", body: JSON.stringify(payload) }),
  removeOutsourcedItem: (quotationId: number, itemId: number) =>
    rawFetch<QuotationCartOrDetail>(`/quotations/${quotationId}/outsourced-items/${itemId}`, { method: "DELETE" }),

  // ---- Admin/Manager: Fulfillment Drawer (approved -> checked out) ----
  fulfillmentQueue: () => tryLoad(async () => (await rawFetch<{ items: FulfillmentQueueRow[] }>("/quotations/fulfillment-queue")).items, []),
  // `outsourceShortfallItems` pre-authorizes specific inventory-backed lines
  // (optionally split across more than one external source) to be sourced
  // externally rather than blocking the whole checkout if the row-locked
  // stock check at this exact moment finds them genuinely short -- see
  // QuotationCheckoutRequest/bulk_checkout_quotation() on the backend.
  checkout: (quotationId: number, outsourceShortfallItems: QuotationOutsourceShortfallItem[] = []) =>
    rawFetch<{ message?: string }>(`/quotations/${quotationId}/checkout`, { method: "POST", body: JSON.stringify({ outsource_shortfall_items: outsourceShortfallItems }) }),

  // ---- Admin-only: global VAT setting ----
  getVat: () => rawFetch<{ vat_percent: number }>("/settings/vat"),
  setVat: (vatPercent: number) => rawFetch<{ vat_percent: number }>("/settings/vat", { method: "PUT", body: JSON.stringify({ vat_percent: vatPercent }) }),

  // ---- PDF export links (not JSON round trips -- each streams a file
  // directly, same pattern as assetsApi.exportUrl). Ported from the legacy
  // frontend's components/exports.js: exportQuotation() for the caller's
  // current draft, exportMyQuoteDetail()/exportQuoteDetail() for one
  // submitted Quotation by ID (self-service vs Admin/Manager). PDF only --
  // unlike the directory/custody exports above, the backend never
  // accepted a `format` query param here (a Quotation is a formatted
  // document, not a tabular row set). ----
  exportCartUrl: () => `${API_BASE}/quotations/export`,
  exportMyQuoteUrl: (quotationId: number) => `${API_BASE}/quotations/me/${quotationId}/export`,
  exportQuoteUrl: (quotationId: number) => `${API_BASE}/quotations/${quotationId}/export`,
};

// Mirrors the legacy frontend's js/ui.js formatPrice()/setCurrencyCode() --
// defaults to Naira, overridable at runtime once quotationsApi.publicConfig()
// resolves the real deployment value (settings.CURRENCY_CODE).
let _currencyCode = "NGN";

export function setCurrencyCode(code: string | null | undefined) {
  if (code) _currencyCode = code;
}

export function formatPrice(price: number | null | undefined): string {
  if (price === null || price === undefined) return "—";
  try {
    return new Intl.NumberFormat("en-NG", { style: "currency", currency: _currencyCode }).format(price);
  } catch {
    // Unknown/unsupported currency code -- fall back to a plain number
    // rather than letting Intl throw and break the whole render.
    return `${_currencyCode} ${Number(price).toFixed(2)}`;
  }
}

export function relativeTime(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const abs = Math.abs(diff);
  const mins = Math.round(abs / 60000);
  const hours = Math.round(abs / 3600000);
  const days = Math.round(abs / 86400000);
  const suffix = diff >= 0 ? "ago" : "from now";
  if (mins < 60) return `${mins}m ${suffix}`;
  if (hours < 24) return `${hours}h ${suffix}`;
  return `${days}d ${suffix}`;
}

export function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}
