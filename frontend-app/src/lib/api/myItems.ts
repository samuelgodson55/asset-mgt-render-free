/**
 * lib/api/myItems.ts
 * ----------------------
 * self-service: My Items (GET /users/me/items -- any authenticated user).
 */

import type { MyItem } from "../types";
import { API_BASE, qs, rawFetch } from "./core";

export const myItemsApi = {
  // `limit`/`offset` are optional -- the Notification Bell and Dashboard
  // call this with none of these (getting the backend's generous default
  // page, effectively "everything" for one person's custody ledger), while
  // the My Items table (see pages/MyItems.tsx) passes them explicitly for
  // TRUE server-side pagination, same pattern as assetsApi.list/usersApi.list.
  list: (limit?: number, offset?: number, filter?: "overdue" | "due_soon") =>
    rawFetch<{ name: string; department_role?: string | null; assigned_items: MyItem[]; total: number; limit: number; offset: number }>(
      `/users/me/items${qs({ limit, offset, filter })}`
    ),
  // Not a JSON round trip -- GET /users/me/items/export streams the file
  // directly (same pattern as assetsApi.exportUrl below). Ported from the
  // legacy frontend's components/exports.js's exportMyItems().
  exportUrl: (format: "csv" | "pdf") => `${API_BASE}/users/me/items/export${qs({ format })}`,
};
