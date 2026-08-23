/**
 * lib/api/dashboard.ts
 * -----------------------
 * Dashboard/home-page data aggregation and the Notification Bell's alert
 * feeds: one real loader per page's need, combined into `api.*` (kept
 * separate from those methods so getStats()/getNotifications() can reuse
 * getAssets()/getCheckouts()'s real loaders directly instead of
 * re-fetching demo data underneath a live session), plus alertsApi (the
 * bell's small, capped overdue/due-soon feeds).
 */

import {
  mockAssets, mockCheckouts, mockExtensions, mockNotifications, mockStats,
} from "../mock";
import type {
  AssetType, Checkout, ExtensionRequest, MyItem, NotificationItem, DashboardStats,
} from "../types";

import { backendReachable, rawFetch, tryLoad } from "./core";
import { setDueSoonReminderDays } from "./dueSoonReminder";
import { mapAsset, mapCheckout, mapCheckoutAlert, mapExtension } from "./mappers";
import { formatDate } from "./format";
import type { RawAssetType, RawCheckout, RawCheckoutAlert, RawExtensionRequest } from "./mappers";


async function loadAssets(): Promise<AssetType[]> {
  const data = await rawFetch<{ items: RawAssetType[] }>("/assets?limit=1000");
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
  const data = await rawFetch<{ assigned_items: MyItem[] }>("/users/me/items?limit=1000");
  return data.assigned_items ?? [];
}

// Mutable module-level cache of the LAST-SEEN settings.DUE_SOON_REMINDER_DAYS
// value (.env, echoed back by GET /checkouts -- see backend/services/
// checkout_service.py's list_active_checkouts()). Set every time
// loadCheckouts() below actually hits the real endpoint; read by the
// Checkouts page to label its "Due soon" column with the true configured
// window instead of a hardcoded guess. Starts at the backend's own
// documented default (config.py's DUE_SOON_REMINDER_DAYS) so the column
// still reads sensibly before the first successful load.
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
  const data = await rawFetch<{ items: RawCheckout[]; total: number; due_soon_reminder_days: number }>("/checkouts?limit=1000");
  if (typeof data.due_soon_reminder_days === "number") {
    setDueSoonReminderDays(data.due_soon_reminder_days);
  }
  return (data.items ?? []).map(mapCheckout);
}

export async function loadExtensionRequests(): Promise<ExtensionRequest[]> {
  const data = await rawFetch<{ items: RawExtensionRequest[] }>("/checkouts/extension-requests?status=pending&limit=1000");
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
    rawFetch<{ date: string; checkouts: number; returns: number }[]>("/assets/activity"),
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
