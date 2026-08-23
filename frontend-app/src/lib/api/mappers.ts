/**
 * lib/api/mappers.ts
 * ---------------------
 * Translate backend/api/*.py response shapes into the types this UI was
 * designed against (src/lib/types.ts).
 */

import type { AssetType, Checkout, ExtensionRequest } from "../types";

export function poolTag(assetId: number | null | undefined): string {
  return assetId != null ? `POOL-${String(assetId).padStart(4, "0")}` : "OUTSOURCED";
}

export function assetStatus(available: number, total: number): AssetType["status"] {
  if (available <= 0) return "out";
  if (total > 0 && available / total <= 0.25) return "low";
  return "available";
}

export interface RawAssetType {
  id: number;
  name: string;
  category: string | null;
  department: string | null;
  total_quantity: number;
  available_quantity: number;
  price: string | number | null;
}

export function mapAsset(raw: RawAssetType): AssetType {
  const total = raw.total_quantity ?? 0;
  const available = raw.available_quantity ?? 0;
  return {
    id: raw.id,
    name: raw.name,
    category: raw.category ?? null,
    department: raw.department ?? null,
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

export interface RawCheckout {
  checkout_id: number;
  asset_id: number | null;
  asset_name: string;
  assignee_name: string;
  assignee_type: string;
  quantity: number;
  outstanding: number;
  due_date: string | null; // "YYYY-MM-DD", null for an open-ended checkout
  checkout_date?: string | null;
  is_overdue: boolean;
  is_due_soon: boolean;
  entity_id?: number | null;
  entity_type?: "user" | "outsider" | null;
}

export function mapCheckout(raw: RawCheckout): Checkout {
  return {
    id: raw.checkout_id,
    asset_id: raw.asset_id ?? 0,
    asset_name: raw.asset_name,
    tag: poolTag(raw.asset_id),
    quantity: raw.outstanding ?? raw.quantity,
    checked_out_to: raw.assignee_name,
    checked_out_by: raw.assignee_type,
    due_at: raw.due_date ?? "",
    checked_out_at: raw.checkout_date ?? raw.due_date ?? "",
    status: raw.is_overdue ? "overdue" : "active",
    due_soon: raw.is_due_soon,
    entity_id: raw.entity_id ?? null,
    entity_type: raw.entity_type ?? null,
  };
}

export interface RawCheckoutAlert {
  checkout_id: number;
  asset_id: number | null;
  asset_name: string;
  assignee_name: string;
  assignee_type: string;
  quantity: number;
  outstanding: number;
  due_date: string; // "YYYY-MM-DD"
  checkout_date?: string | null;
  entity_id?: number | null;
  entity_type?: "user" | "outsider" | null;
}

export function mapCheckoutAlert(raw: RawCheckoutAlert, status: Checkout["status"]): Checkout {
  return {
    id: raw.checkout_id,
    asset_id: raw.asset_id ?? 0,
    asset_name: raw.asset_name,
    tag: poolTag(raw.asset_id),
    quantity: raw.outstanding ?? raw.quantity,
    checked_out_to: raw.assignee_name,
    checked_out_by: raw.assignee_type,
    due_at: raw.due_date,
    checked_out_at: raw.checkout_date ?? raw.due_date,
    status,
    entity_id: raw.entity_id ?? null,
    entity_type: raw.entity_type ?? null,
  };
}

export interface RawExtensionRequest {
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

export function mapExtension(raw: RawExtensionRequest): ExtensionRequest {
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
