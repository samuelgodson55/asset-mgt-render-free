export interface AssetType {
  id: number;
  name: string;
  category: string | null;
  total_quantity: number;
  available_quantity: number;
  checked_out_quantity: number;
  price: number | null;
  tag: string;
  status: "available" | "low" | "out";
  updated_at: string;
}

export interface Checkout {
  id: number;
  asset_id: number;
  asset_name: string;
  tag: string;
  quantity: number;
  checked_out_to: string;
  checked_out_by: string;
  due_at: string;
  checked_out_at: string;
  status: "active" | "returned" | "overdue";
}

export interface ExtensionRequest {
  id: number;
  checkout_id: number;
  asset_name: string;
  requested_by: string;
  requested_until: string;
  reason: string;
  status: "pending" | "approved" | "denied";
}

export interface NotificationItem {
  id: number;
  title: string;
  body: string;
  kind: "overdue" | "extension" | "system" | "low_stock";
  created_at: string;
  read: boolean;
}

// ---------------------------------------------------------------------------
// Admin: System Backups (true Super Admin only -- see backend's
// deps.require_true_super_admin / api/backup_api.py) and Inventory Import
// (Super Admin or Admin -- deps.require_super_admin / assets_api.py's
// POST /assets/import).
// ---------------------------------------------------------------------------

export interface BackupEntry {
  filename: string;
  created_at: string;
  size_bytes: number;
  triggered_by: "manual" | "scheduled" | "pre_restore_safety";
  gdrive_uploaded: boolean;
  gdrive_error?: string | null;
}

export interface BackupStatus {
  auto_backup_enabled: boolean;
  backup_hours_display: string[];
  display_timezone_label: string;
  gdrive_enabled: boolean;
  backup_count: number;
  retention_count: number;
  latest_backup: BackupEntry | null;
}

export interface RestoreCredentialReconciliation {
  super_admins_reset?: number;
  users_reinserted?: number;
}

export interface RestoreOutsiderReconciliation {
  outsiders_reinserted?: number;
}

export interface RestoreAssetActivityReconciliation {
  checkouts_reconciled?: number;
  checkouts_reinserted?: number;
  checkouts_skipped?: number;
  quotations_reconciled?: number;
  quotations_reinserted?: number;
  quotations_skipped?: number;
}

export interface RestoreResult {
  message?: string;
  credential_reconciliation?: RestoreCredentialReconciliation;
  outsider_reconciliation?: RestoreOutsiderReconciliation;
  asset_activity_reconciliation?: RestoreAssetActivityReconciliation;
}

export interface ImportRowError {
  row: number;
  name?: string;
  reason: string;
}

export interface ImportResult {
  imported_count: number;
  errors: ImportRowError[];
  message?: string;
}

export interface MyItem {
  checkout_id: number;
  asset_name: string;
  quantity: number;
  checkout_date: string;
  due_date: string;
  due_soon: boolean;
}

export interface ProfileDetail {
  id: number;
  name: string;
  email: string;
  username: string | null;
  role: string;
  department?: string | null;
  department_role?: string | null;
}

export interface PersonAlerts {
  overdue: boolean;
  due_soon: boolean;
  pending_extension: boolean;
}

export interface UserRow {
  id: number;
  name: string;
  email: string;
  phone_number?: string | null;
  username?: string | null;
  role: string;
  department?: string | null;
  department_role?: string | null;
  checkout_count: number;
  alerts: PersonAlerts;
}

export interface OutsiderRow {
  id: number;
  name: string;
  email?: string | null;
  phone_number?: string | null;
  company?: string | null;
  outstanding_items: number;
  alerts: PersonAlerts;
}

export interface CustodyItem {
  checkout_id: number;
  asset_name: string;
  quantity: number;
  outstanding: number;
  checkout_date?: string | null;
  due_date: string;
  due_soon: boolean;
  overdue: boolean;
}

export interface AuditLogEntry {
  id: number;
  operator: string;
  action: string;
  target_type: string;
  target_id: number;
  details: string;
  timestamp: string;
}

export interface DashboardStats {
  total_assets: number;
  available: number;
  checked_out: number;
  overdue: number;
  due_soon: number;
  low_stock: number;
  categories: { name: string; count: number }[];
  activity: { date: string; checkouts: number; returns: number }[];
}
