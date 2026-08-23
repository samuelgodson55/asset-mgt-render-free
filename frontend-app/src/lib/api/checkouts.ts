/**
 * lib/api/checkouts.ts
 * -------------------------
 * The Checkouts page: paginated active-checkout table (all four tabs),
 * returns, direct extend, and bulk extend. require_privileged_role for
 * every mutation (Super Admin/Admin/Manager).
 */

import type { BulkExtendResult } from "../types";
import { qs, rawFetch } from "./core";
import { setDueSoonReminderDays } from "./dueSoonReminder";
import { mapCheckout } from "./mappers";
import type { RawCheckout } from "./mappers";


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
        setDueSoonReminderDays(data.due_soon_reminder_days);
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
