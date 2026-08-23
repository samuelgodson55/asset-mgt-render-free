/**
 * lib/api/extensions.ts
 * -------------------------
 * self-service: Extension Requests (request/review/decide -- backend/api/
 * checkouts_api.py). Review/decide is require_privileged_role (Super
 * Admin/Admin/Manager); requesting is open to whoever holds the checkout.
 */

import { mockExtensions } from "../mock";
import type { MyExtensionDecision } from "../types";
import { qs, rawFetch, tryLoad } from "./core";
import { loadExtensionRequests } from "./dashboard";
import { mapExtension } from "./mappers";
import type { RawExtensionRequest } from "./mappers";

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
