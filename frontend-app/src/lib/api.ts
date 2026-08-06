import { mockAssets, mockCheckouts, mockExtensions, mockNotifications, mockStats, mockBackups, mockBackupStatus, mockDigestRecipients } from "./mock";
import type { AssetType, Checkout, ExtensionRequest, NotificationItem, DashboardStats, BackupEntry, BackupStatus, RestoreResult, ImportResult, MyItem, ProfileDetail, UserRow, OutsiderRow, CustodyItem, AuditLogEntry } from "./types";

// Points at the FastAPI backend. In production this app is built with
// `base: '/app/'` and served by the same nginx that proxies `/api/*` to
// the backend (see nginx/default.conf.template's `location /api/` block),
// so the default here is a same-origin relative path -- no CORS, and the
// httpOnly `access_token` session cookie set by POST /auth/login is sent
// automatically. For local `npm run dev` against a bare `uvicorn main:app`
// on another port, set VITE_API_BASE_URL in `.env.local` instead (see
// README.md).
const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "/api").replace(/\/$/, "");

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

/** Runs a real loader; on any failure (network down, 401, 403, ...) falls back to demo data so the UI stays fully explorable. */
async function tryLoad<T>(loader: () => Promise<T>, fallback: T): Promise<T> {
  try {
    const data = await loader();
    backendReachable = true;
    return data;
  } catch {
    backendReachable = false;
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

interface RawCheckoutAlert {
  checkout_id: number;
  asset_id: number | null;
  asset_name: string;
  assignee_name: string;
  assignee_type: string;
  quantity: number;
  outstanding: number;
  due_date: string; // "YYYY-MM-DD"
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

async function loadCheckouts(): Promise<Checkout[]> {
  // The backend has no single "list every active checkout" route (custody
  // is tracked per-user/-outsider via GET /users/{id}/items instead) --
  // the overdue + due-soon alert feeds are the closest real analogue to a
  // system-wide checkouts table, and are exactly what admin.html's own
  // dashboard alerts are built from (see backend/api/checkouts_api.py).
  const [overdue, dueSoon] = await Promise.all([loadOverdue(), loadDueSoon()]);
  return [...overdue, ...dueSoon];
}

async function loadExtensionRequests(): Promise<ExtensionRequest[]> {
  const data = await rawFetch<{ items: RawExtensionRequest[] }>("/checkouts/extension-requests?status=pending&limit=100");
  return (data.items ?? []).map(mapExtension);
}

async function loadNotifications(): Promise<NotificationItem[]> {
  // There's no in-app notification-feed endpoint on the backend today
  // (api/notifications_api.py is only the digest-email recipient
  // settings) -- same as the legacy frontend's notification bell
  // (js/components/notifications.js), this is synthesized client-side
  // from the overdue/due-soon/extension-request feeds.
  const [overdue, dueSoon, extensions] = await Promise.all([
    loadOverdue(),
    loadDueSoon(),
    loadExtensionRequests(),
  ]);

  const items: NotificationItem[] = [
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

async function loadStats(): Promise<DashboardStats> {
  const [assets, overdue, dueSoon] = await Promise.all([loadAssets(), loadOverdue(), loadDueSoon()]);

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
    overdue: overdue.length,
    due_soon: dueSoon.length,
    low_stock: lowStock,
    categories: Array.from(byCategory, ([name, count]) => ({ name, count })),
    // No historical checkout/return time-series endpoint exists on the
    // backend yet -- left empty (rather than fabricated) when live; the
    // chart just renders blank until one is added (e.g. GET
    // /assets/activity), same spirit as the activity comment above.
    activity: [],
  };
}

export const api = {
  isLive: () => backendReachable === true,
  getAssets: () => tryLoad(loadAssets, mockAssets),
  getCheckouts: () => tryLoad(loadCheckouts, mockCheckouts),
  getExtensionRequests: () => tryLoad(loadExtensionRequests, mockExtensions),
  getNotifications: () => tryLoad(loadNotifications, mockNotifications),
  getStats: () => tryLoad(loadStats, mockStats),
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
  list: () => rawFetch<{ name: string; department_role?: string | null; assigned_items: MyItem[] }>("/users/me/items"),
};

export const extensionsApi = {
  request: (checkoutId: number, newDueDate: string, reason: string) =>
    rawFetch<{ message?: string }>(`/checkouts/${checkoutId}/extension-requests`, {
      method: "POST",
      body: JSON.stringify({ new_due_date: newDueDate, reason: reason || null }),
    }),
  listPending: () => tryLoad(loadExtensionRequests, []),
  decide: (requestId: number, approve: boolean, note: string | null) =>
    rawFetch<{ message?: string }>(`/checkouts/extension-requests/${requestId}/decision`, {
      method: "POST",
      body: JSON.stringify({ approve, note }),
    }),
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
  updateIdentity: (name: string, email: string, username: string, currentPassword: string) =>
    rawFetch<ProfileDetail & { message?: string }>("/auth/me", {
      method: "PATCH",
      body: JSON.stringify({ name, email, username, current_password: currentPassword }),
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
  update: (id: number, req: Partial<{ name: string; username: string; email: string; phone_number: string }>) =>
    rawFetch<UserRow>(`/users/${id}`, { method: "PATCH", body: JSON.stringify(req) }),
  resetPassword: (id: number, newPassword: string, adminPassword: string) =>
    rawFetch<{ message?: string }>(`/users/${id}/reset-password`, { method: "POST", body: JSON.stringify({ new_password: newPassword, admin_password: adminPassword }) }),
  remove: (id: number) => rawFetch<void>(`/users/${id}`, { method: "DELETE" }),
  restore: (id: number) => rawFetch<{ message?: string }>(`/users/${id}/restore`, { method: "POST" }),
  listDeleted: (limit: number, offset: number, search: string) => rawFetch<DirectoryPage<UserRow>>(`/users/deleted${qs({ limit, offset, search })}`),
  items: (id: number) => rawFetch<{ name: string; assigned_items: CustodyItem[] }>(`/users/${id}/items`),
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
  items: (id: number) => rawFetch<{ name: string; assigned_items: CustodyItem[] }>(`/outsiders/${id}/items`),
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
// checkouts: return processing, shared by the Custody Ledger drawer for
// both the User Directory and the Ad-Hoc Directory (require_privileged_role).
// ---------------------------------------------------------------------------

export const checkoutsApi = {
  returnItem: (checkoutId: number, quantity: number) => rawFetch<{ message?: string }>(`/checkouts/${checkoutId}/return`, { method: "POST", body: JSON.stringify({ quantity }) }),
};

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
