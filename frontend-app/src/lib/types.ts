export interface AssetType {
  id: number;
  name: string;
  category: string | null;
  department: string | null;
  total_quantity: number;
  available_quantity: number;
  checked_out_quantity: number;
  price: number | null;
  tag: string;
  status: "available" | "low" | "out";
  updated_at: string;
}

// ---------------------------------------------------------------------------
// Asset Inventory core -- Properties Hub / Pool Details, Dispatch, Restore
// Deleted Assets. Backed by backend/api/assets_api.py +
// backend/services/asset_service.py. See lib/api.ts's assetsApi.
// ---------------------------------------------------------------------------

export interface AssetActiveAssignment {
  checkout_id: number;
  assignee_name: string;
  assignee_type: string;
  quantity: number;
  quantity_returned: number;
  outstanding: number;
  checkout_date: string | null;
  due_date: string;
}

export interface AssetExceptionItem {
  exception_id: number;
  serial: string;
  notes: string | null;
}

export interface AssetDetails {
  asset_id: number;
  name: string;
  category: string | null;
  department: string | null;
  price: number | null;
  total_quantity: number;
  available_quantity: number;
  outbound_quantity: number;
  isolated_quantity: number;
  under_repair_count: number;
  under_repair_items: AssetExceptionItem[];
  stolen_count: number;
  stolen_items: AssetExceptionItem[];
  active_assignments: AssetActiveAssignment[];
}

export interface DeletedAssetRow {
  id: number;
  name: string;
  category: string | null;
  department: string | null;
  total_quantity: number;
  price: number | null;
  deleted_at: string | null;
}

// Populates the Dispatch drawer's "Assign To" dropdowns -- a narrow view
// of UserRow (see users.js's loadUsers()), not the full directory shape.
export interface RosterUser {
  id: number;
  name: string;
  email: string;
  role: string;
  department_role?: string | null;
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
  // Mirrors backend/services/checkout_service.py's list_active_checkouts()
  // `is_due_soon` -- true when this checkout's due date falls within the
  // environment-configured settings.DUE_SOON_REMINDER_DAYS window (.env)
  // and it isn't already overdue. Only populated by GET /checkouts (the
  // "All"-tab loader); the overdue/due-soon alert feeds don't need it
  // since each of those is already implicitly one or the other.
  due_soon?: boolean;
  // Who currently holds this checkout, in a form the Notification Bell can
  // group by and click through to that person's Custody Ledger -- see
  // backend/services/checkout_service.py's list_overdue_checkouts()/
  // list_due_soon_checkouts(). Null/undefined when the holder record was
  // deleted out from under an active checkout (legacy's "Unknown" case).
  entity_id?: number | null;
  entity_type?: "user" | "outsider" | null;
}

export interface ExtensionRequest {
  id: number;
  checkout_id: number;
  asset_name: string;
  requested_by: string;
  requested_until: string;
  reason: string;
  status: "pending" | "approved" | "denied";
  // Same grouping/click-through fields as Checkout above, plus the bare
  // holder name (assignee_name) -- requested_by is a longer, decorated
  // label ("Jane Doe (jane@x.com) -- logged by admin@x.com") not suited
  // for a "Jane Doe has 2 pending requests" grouped heading.
  entity_id?: number | null;
  entity_type?: "user" | "outsider" | null;
  assignee_name?: string;
}

// Self-service feed: GET /checkouts/my-extension-decisions -- recently
// approved/denied decisions on the CALLER's own extension requests. See
// backend/services/extension_service.py's list_my_recent_extension_decisions().
export interface MyExtensionDecision {
  id: number;
  checkout_id: number;
  asset_name: string;
  status: "approved" | "denied";
  requested_new_due_date: string | null;
  due_date: string | null;
  decision_note: string | null;
  decided_at: string | null;
}

// Self-service feed: GET /quotations/me/notifications -- "assigned"/
// "updated" alerts about the caller's own Quotations, written by
// backend/services/quotation_service.py's _notify_quotation_recipient()
// whenever an Admin/Manager assigns or changes a quote that belongs to
// this person. See lib/api.ts's quotationsApi.myNotifications().
export interface QuotationNotification {
  id: number;
  quotation_id: number;
  reference_number: string | null;
  kind: "assigned" | "updated";
  message: string;
  created_by: string | null;
  created_at: string;
  read_at: string | null;
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
  updated_count?: number;
  created_count?: number;
  error_count?: number;
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
  // Backend's _group_assigned_items() (services/user_service.py) already
  // computes this alongside due_soon -- surfaced here so the Dashboard's
  // "Fleet by category" card can group a Staff/Customer session's own
  // items by category the same way it groups org-wide totals for a
  // privileged session, instead of only being usable by the CSV/PDF export.
  asset_category?: string | null;
  // Backend's _group_assigned_items() already computes these alongside
  // due_soon (see services/user_service.py) -- surfaced here so the
  // Notification Bell's personal alert sections (My overdue / My pending
  // extension requests) don't need a second round trip.
  overdue?: boolean;
  pending_extension?: boolean;
}

export interface ProfileDetail {
  id: number;
  name: string;
  email: string;
  username: string | null;
  phone_number?: string | null;
  company?: string | null;
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
  company?: string | null;
  username?: string | null;
  role: string;
  department?: string | null;
  department_role?: string | null;
  checkout_count: number;
  alerts: PersonAlerts;
}

// Row shape returned by GET /users/deleted -- a soft-deleted account, no
// custody count/alerts (a deleted account can't hold custody), plus
// deleted_at so the Restore Deleted Users panel can show when it happened.
export interface DeletedUserRow {
  id: number;
  name: string;
  email: string;
  username?: string | null;
  role: string;
  department?: string | null;
  department_role?: string | null;
  deleted_at: string | null;
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
  // Populated when a self-service extension request is awaiting a
  // decision on this specific checkout -- lets the Custody Ledger swap
  // that row's "Extend" button for Approve/Deny acting on THAT request,
  // instead of firing off a brand new, unrelated direct extension. See
  // backend/services/user_service.py & outsider_service.py's
  // _pending_extension_fields().
  pending_extension?: boolean;
  pending_extension_request_id?: number | null;
  pending_extension_new_due_date?: string | null;
  pending_extension_reason?: string | null;
  is_outsourced?: boolean;
  outsourced_source?: string | null;
}

export interface BulkExtendResultLine {
  checkout_id: number;
  success: boolean;
  error?: string | null;
}

export interface BulkExtendResult {
  message?: string;
  succeeded: number;
  failed: number;
  results: BulkExtendResultLine[];
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

// ---------------------------------------------------------------------------
// Quotation feature -- ported from the legacy frontend's
// js/components/quotation.js. Backed by backend/api/quotations_api.py +
// backend/services/quotation_service.py. See lib/api.ts's quotationsApi
// for the HTTP calls and pages/Quotations.tsx / pages/Admin.tsx's
// QuotesPanel for where these are used.
// ---------------------------------------------------------------------------

export interface PublicConfig {
  currency_code: string;
  site_name: string;
  // Server-side toggle (settings.CATALOG_SHOW_STOCK_TO_STAFF_CUSTOMER) --
  // catalog rows never lie about their own available_quantity/status either
  // way, this just says whether the UI should render those columns for a
  // Staff/Customer session (Manager/Admin/Super Admin always see stock,
  // regardless of this flag -- see lib/roles.ts's canSeeStock()).
  //
  // NOTE: this field name must match backend/services/quotation_service.py's
  // get_public_config() exactly ("show_stock_to_staff_customer") -- it
  // previously read `show_stock`, which no real response ever contained,
  // so every consumer silently read `undefined` from a live backend.
  show_stock_to_staff_customer?: boolean;
  maintenance_mode: boolean;
  maintenance_message: string;
}

export interface CatalogAsset {
  id: number;
  name: string;
  category: string | null;
  department: string | null;
  price: number | null;
  // Omitted by the backend entirely (not just zeroed) for a Staff/Customer
  // session when CATALOG_SHOW_STOCK_TO_STAFF_CUSTOMER is off -- see
  // services/quotation_service.py's list_catalog(). Genuinely optional,
  // not just possibly-zero, so callers must check for presence rather
  // than falsiness before rendering either field.
  available_quantity?: number;
  status?: "In Stock" | "Out of Stock" | string;
}

export interface QuotationLineItem {
  // Present on a regular catalog-backed line; absent (use
  // outsourced_item_id instead) on an Admin/Manager-added outsourced line.
  item_id?: number;
  outsourced_item_id?: number;
  asset_name: string;
  category?: string | null;
  quantity: number;
  start_date: string;
  due_date: string;
  days: number;
  line_total: number;
  is_outsourced?: boolean;
  sourced_from?: string | null;
  available_quantity?: number;
  stock_shortfall?: boolean;
  shortfall_quantity?: number;
  unit_price?: number | null;
}

export interface QuotationPartyRef {
  id: number;
  name: string;
  email?: string;
  company?: string | null;
}

export type QuotationStatus = "draft" | "submitted" | "approved" | "fulfilled" | "paid";

export interface QuotationCartOrDetail {
  id?: number;
  reference_number?: string;
  status?: QuotationStatus;
  items: QuotationLineItem[];
  subtotal: number;
  vat_percent: number;
  vat_amount: number;
  discount_percent?: number;
  discount_amount?: number;
  total: number;
  notes?: string | null;
  submitted_at?: string;
  approved_at?: string;
  fulfilled_at?: string | null;
  paid_at?: string | null;
  payment_method?: string | null;
  payment_reference?: string | null;
  paid_by?: QuotationPartyRef | null;
  requester?: QuotationPartyRef;
  assigned_to?: QuotationPartyRef | null;
  assigned_outsider?: QuotationPartyRef | null;
  is_personal_request?: boolean;
  locked?: boolean;
}

export interface QuotationListRow {
  id: number;
  reference_number: string;
  status: QuotationStatus;
  requester?: QuotationPartyRef | null;
  submitted_at: string;
  item_count: number;
  total: number;
  assigned_to?: QuotationPartyRef | null;
  assigned_outsider?: QuotationPartyRef | null;
  locked?: boolean;
  paid_at?: string | null;
}

// ---- Admin/Manager: Fulfillment Drawer per-line shortfall splitting ----
// One external source's share of a line's stock shortfall (mirrors
// backend/schemas/quotations_schema.py's QuotationOutsourceAllocation) --
// letting a shortfall be split across more than one outsourcing vendor.
export interface QuotationOutsourceAllocation {
  quantity: number;
  sourced_from?: string | null;
  unit_price?: number | null;
}

// One line's "source the shortfall externally instead" instruction, sent
// as part of POST /quotations/{id}/checkout's outsource_shortfall_items.
export interface QuotationOutsourceShortfallItem {
  quotation_item_id: number;
  allocations: QuotationOutsourceAllocation[];
}

// Admin/Manager-only "not currently in inventory" line -- POST
// /quotations/{id}/outsourced-items.
export interface QuotationOutsourcedItemCreate {
  name: string;
  description?: string | null;
  unit_price: number;
  quantity: number;
  sourced_from?: string | null;
  start_date: string;
  due_date: string;
}

export interface FulfillmentQueueRow {
  id: number;
  reference_number: string;
  total: number;
  item_count: number;
  approved_at: string;
  checkout_to?: { name: string; email: string } | null;
  has_shortfall?: boolean;
  items: QuotationLineItem[];
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

// ---------------------------------------------------------------------------
// Reporting / Analytics dashboard -- Manager/Admin only
// (require_privileged_role). Backed by backend/api/reports_api.py +
// backend/services/reports_service.py's get_dashboard(). See lib/api.ts's
// reportsApi and pages/Reports.tsx.
// ---------------------------------------------------------------------------

export interface UtilizationRow {
  asset_type_id: number;
  name: string;
  category: string | null;
  department: string | null;
  total_quantity: number;
  available_quantity: number;
  currently_checked_out: number;
  utilization_rate: number | null;
  checkout_count: number;
  total_checkout_days: number;
}

export interface OverdueTrendPoint {
  month: string;
  label: string;
  overdue_count: number;
}

export interface OverdueBreakdownRow {
  name?: string;
  department?: string;
  overdue_count: number;
}

export interface OverdueReport {
  trend: OverdueTrendPoint[];
  total_overdue_now: number;
  by_asset_type: OverdueBreakdownRow[];
  by_department: OverdueBreakdownRow[];
}

export interface SpendRow {
  category?: string;
  department?: string;
  total_spend: number;
  item_count: number;
}

export interface SpendReport {
  by_category: SpendRow[];
  by_department: SpendRow[];
  priced_checkout_count: number;
  unpriced_checkout_count: number;
}

export interface RevenueByDepartmentRow {
  department: string;
  total_revenue: number;
  item_count: number;
  quotation_count: number;
}

export interface RevenueReport {
  by_department: RevenueByDepartmentRow[];
  total_revenue: number;
  fulfilled_quotation_count: number;
  priced_line_count: number;
  unassigned_line_count: number;
}

export interface QuotationTurnaroundMonth {
  month: string;
  label: string;
  avg_submit_to_fulfill_hours: number | null;
  sample_size: number;
}

export interface QuotationTurnaroundReport {
  avg_submit_to_approve_hours: number | null;
  sample_size_submit_to_approve: number;
  avg_approve_to_fulfill_hours: number | null;
  sample_size_approve_to_fulfill: number;
  avg_submit_to_fulfill_hours: number | null;
  sample_size_submit_to_fulfill: number;
  total_quotations_submitted: number;
  by_month: QuotationTurnaroundMonth[];
}

export interface ReportsDashboard {
  period: { start_date: string | null; end_date: string | null };
  utilization_by_asset_type: UtilizationRow[];
  overdue: OverdueReport;
  spend: SpendReport;
  revenue: RevenueReport;
  quotation_turnaround: QuotationTurnaroundReport;
}

