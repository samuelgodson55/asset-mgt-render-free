/**
 * lib/api/directory.ts
 * ------------------------
 * admin: User Directory (backend/api/users_api.py) -- list/search is
 * require_privileged_role (Super Admin/Admin/Manager); delete/restore/
 * purge/reset-password are require_super_admin only. Also Ad-Hoc/Outsider
 * Directory, Audit Log, and Reports -- all admin-only reads over the same
 * shape of paginated endpoint.
 */

import { mockAuditLogs, mockReportsDashboard } from "../mock";
import type { AuditLogEntry, CustodyItem, DeletedUserRow, OutsiderRow, ReportsDashboard, UserRow } from "../types";
import { API_BASE, qs, rawFetch, tryLoad } from "./core";
import type { DirectoryPage } from "./core";


export const usersApi = {
  list: (limit: number, offset: number, search: string) => rawFetch<DirectoryPage<UserRow>>(`/users${qs({ limit, offset, search })}`),
  create: (req: { name: string; email: string; phone_number?: string; role: string; password: string; department?: string; department_role?: string }) =>
    rawFetch<UserRow>("/users", { method: "POST", body: JSON.stringify(req) }),
  update: (id: number, req: Partial<{ name: string; username: string; email: string; phone_number: string; company: string; role: string }>) =>
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
  list: (limit: number, offset: number) =>
    tryLoad(
      () => rawFetch<DirectoryPage<AuditLogEntry>>(`/audit-logs${qs({ limit, offset })}`),
      { items: mockAuditLogs.slice(offset, offset + limit), total: mockAuditLogs.length, limit, offset }
    ),
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
  revenue: (startDate?: string, endDate?: string) =>
    rawFetch<ReportsDashboard["revenue"]>(`/reports/revenue${qs({ start_date: startDate, end_date: endDate })}`),
};
