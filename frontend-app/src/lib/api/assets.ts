/**
 * lib/api/assets.ts
 * ----------------------
 * Asset Inventory core -- ported from the legacy frontend's
 * js/components/assets.js. List/details/categories/export are open to any
 * authenticated user (get_current_user); everything that mutates a pool
 * (create/rename/recategorize/reprice/capacity/delete/restore/purge/
 * exception/recall) is require_super_admin; dispatch (checkout_advanced)
 * is require_privileged_role (Super Admin/Admin/Manager). Also
 * maintenanceApi -- pulling a serial out of service / recalling it.
 */

import { mockAssets } from "../mock";
import type { AssetDetails, AssetType, DeletedAssetRow, OutsiderRow, RosterUser } from "../types";
import { API_BASE, qs, rawFetch, tryLoad } from "./core";
import type { DirectoryPage } from "./core";
import { mapAsset } from "./mappers";
import type { RawAssetType } from "./mappers";


export interface AssetTypeCreateRequest {
  name: string;
  total_quantity: number;
  category?: string | null;
  department?: string | null;
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
  list: (limit: number, offset: number, search = "", category?: string, status?: "available" | "low" | "out"): Promise<DirectoryPage<AssetType>> =>
    tryLoad(async () => {
      const data = await rawFetch<{ items: RawAssetType[]; total: number; limit: number; offset: number }>(`/assets${qs({ limit, offset, search, category, status })}`);
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
      if (status) {
        filtered = filtered.filter((a) => a.status === status);
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
  updateDepartment: (assetId: number, department: string | null) => rawFetch<{ message?: string }>(`/assets/${assetId}/department`, { method: "PUT", body: JSON.stringify({ department }) }),
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
// Maintenance mode -- backed by backend/api/maintenance_api.py +
// backend/services/maintenance_service.py. `status()` is intentionally
// callable while logged out (see that route's own docstring: it powers
// MaintenanceGate.tsx's public overlay); `update()` requires the real
// Super Admin session server-side regardless of what the UI shows.
// ---------------------------------------------------------------------------

export const maintenanceApi = {
  status: () => rawFetch<{ enabled: boolean; message: string; updated_at?: string | null; updated_by?: string | null }>("/maintenance/status"),
  update: (payload: { enabled: boolean; message: string }) => rawFetch<{ enabled: boolean; message: string; updated_at?: string | null; updated_by?: string | null }>("/maintenance/status", { method: "PUT", body: JSON.stringify(payload) }),
};
