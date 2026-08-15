import type { AssetType, Checkout, ExtensionRequest, NotificationItem, DashboardStats, BackupEntry, BackupStatus, CatalogAsset, QuotationCartOrDetail, ReportsDashboard, AuditLogEntry } from "./types";

const categories = ["Field Radios", "Optics", "Power", "Networking", "Fabrication", "Safety"];
const assetDepartments = ["Camera", "Lighting", "Grip", "Audio", "Power", "Production"];

const names: Record<string, string[]> = {
  "Field Radios": ["Motorola APX 8000", "Kenwood NX-5300", "Icom F5061D"],
  Optics: ["Vortex Diamondback HD", "Leupold VX-3HD", "Steiner P4Xi"],
  Power: ["Goal Zero Yeti 1500X", "Milwaukee M18 Pack", "EcoFlow Delta Pro"],
  Networking: ["Cradlepoint IBR900", "Ubiquiti Rocket M5", "Cisco Meraki MX67"],
  Fabrication: ["Prusa MK4S", "DeWalt Plasma Cutter", "Bosch GLL Laser Level"],
  Safety: ["MSA Altair 4X", "3M Versaflo TR-600", "Petzl ANTI Harness"],
};

function tagFor(id: number, cat: string) {
  const prefix = cat.slice(0, 3).toUpperCase();
  return `${prefix}-${String(id).padStart(4, "0")}`;
}

export const mockAssets: AssetType[] = categories.flatMap((cat, ci) =>
  names[cat].map((name, ni) => {
    const id = ci * 10 + ni + 1;
    const total = [12, 8, 20, 5, 3, 15][((id * 3) % 6)];
    const checkedOut = Math.max(0, Math.round(total * ((id % 5) / 6)));
    const available = total - checkedOut;
    const status: AssetType["status"] = available === 0 ? "out" : available / total < 0.25 ? "low" : "available";
    return {
      id,
      name,
      category: cat,
      department: assetDepartments[ci],
      total_quantity: total,
      available_quantity: available,
      checked_out_quantity: checkedOut,
      price: [149, 899, 1299, 349, 4200, 210][id % 6],
      tag: tagFor(id, cat),
      status,
      updated_at: new Date(Date.now() - id * 86400000).toISOString(),
    };
  })
);

const people = ["R. Nakamura", "T. Adeyemi", "S. Kowalski", "M. Fontaine", "J. Osei", "A. Whitfield"];

export const mockCheckouts: Checkout[] = mockAssets
  .filter((a) => a.checked_out_quantity > 0)
  .map((a, i) => {
    const dueOffset = (i % 5) - 2; // some overdue, some due soon, some fine
    const due = new Date(Date.now() + dueOffset * 86400000);
    return {
      id: 100 + i,
      asset_id: a.id,
      asset_name: a.name,
      tag: a.tag,
      quantity: a.checked_out_quantity,
      checked_out_to: people[i % people.length],
      checked_out_by: people[(i + 2) % people.length],
      due_at: due.toISOString(),
      checked_out_at: new Date(Date.now() - (7 - i) * 86400000).toISOString(),
      status: dueOffset < 0 ? "overdue" : "active",
    };
  });

export const mockExtensions: ExtensionRequest[] = mockCheckouts.slice(0, 3).map((c, i) => ({
  id: 200 + i,
  checkout_id: c.id,
  asset_name: c.asset_name,
  requested_by: c.checked_out_to,
  requested_until: new Date(Date.now() + (5 + i) * 86400000).toISOString(),
  reason: ["Field deployment extended by client", "Awaiting replacement unit", "Training extended one week"][i],
  status: "pending",
}));

export const mockNotifications: NotificationItem[] = [
  { id: 1, title: "3 checkouts overdue", body: "Field Radios and Optics pool items are past due for return.", kind: "overdue", created_at: new Date(Date.now() - 3600e3).toISOString(), read: false },
  { id: 2, title: "Extension requested", body: "R. Nakamura requested an extension on Vortex Diamondback HD.", kind: "extension", created_at: new Date(Date.now() - 7200e3).toISOString(), read: false },
  { id: 3, title: "Low stock: Fabrication", body: "Prusa MK4S pool has fallen below 25% availability.", kind: "low_stock", created_at: new Date(Date.now() - 26 * 3600e3).toISOString(), read: true },
  { id: 4, title: "Import completed", body: "42 asset rows imported from quarterly-inventory.csv.", kind: "system", created_at: new Date(Date.now() - 50 * 3600e3).toISOString(), read: true },
];

export const mockBackups: BackupEntry[] = [
  { filename: "ledger-backup-2026-08-06T0300Z.sql.gz", created_at: new Date(Date.now() - 5 * 3600e3).toISOString(), size_bytes: 18_400_000, triggered_by: "scheduled", gdrive_uploaded: true },
  { filename: "ledger-backup-2026-08-05T0300Z.sql.gz", created_at: new Date(Date.now() - 29 * 3600e3).toISOString(), size_bytes: 18_100_000, triggered_by: "scheduled", gdrive_uploaded: true },
  { filename: "ledger-backup-2026-08-04T1512Z.sql.gz", created_at: new Date(Date.now() - 40 * 3600e3).toISOString(), size_bytes: 17_950_000, triggered_by: "manual", gdrive_uploaded: false, gdrive_error: "Drive quota exceeded" },
  { filename: "ledger-backup-2026-08-04T0300Z.sql.gz", created_at: new Date(Date.now() - 53 * 3600e3).toISOString(), size_bytes: 17_900_000, triggered_by: "scheduled", gdrive_uploaded: true },
];

export const mockBackupStatus: BackupStatus = {
  auto_backup_enabled: true,
  backup_hours_display: ["03:00"],
  display_timezone_label: "UTC",
  gdrive_enabled: true,
  backup_count: mockBackups.length,
  retention_count: 14,
  latest_backup: mockBackups[0],
};

export const mockDigestRecipients: string[] = ["ops@ledger.example.com"];


export const mockAuditLogs: AuditLogEntry[] = [
  {
    id: 1,
    target_type: "AssetType",
    target_id: 1,
    timestamp: new Date(Date.now() - 30 * 60e3).toISOString(),
    operator: "r.adeyemi@corp.io",
    action: "POOL_CREATED",
    details: "Created asset pool 'MacBook Pro 14\" M3 Pool' with initial quantity of 15.",
  },
  {
    id: 2,
    target_type: "AssetType",
    target_id: 1,
    timestamp: new Date(Date.now() - 65 * 60e3).toISOString(),
    operator: "s.chen@corp.io",
    action: "CHECKOUT",
    details: "Assigned 1 unit of 'MacBook Pro 14\" M3 Pool' to Staff: T. Okafor.",
  },
  {
    id: 3,
    target_type: "User",
    target_id: 4,
    timestamp: new Date(Date.now() - 2 * 3600e3).toISOString(),
    operator: "r.adeyemi@corp.io",
    action: "USER_PROVISIONED",
    details: "Created account for A. Bello (staff).",
  },
  {
    id: 4,
    target_type: "AssetCheckout",
    target_id: 102,
    timestamp: new Date(Date.now() - 4 * 3600e3).toISOString(),
    operator: "s.chen@corp.io",
    action: "EXTENSION_REQUESTED",
    details: "Requested a return-date extension for a checkout.",
  },
];

// Demo data for the self-service Quotation Catalog/cart (see
// pages/Quotations.tsx) -- reuses mockAssets' own names/prices/stock so
// "Demo browsing" shows the same inventory everywhere in the app.
export const mockCatalog: CatalogAsset[] = mockAssets.map((a) => ({
  id: a.id,
  name: a.name,
  category: a.category,
  department: a.department,
  price: a.price,
  available_quantity: a.available_quantity,
  status: a.available_quantity > 0 ? "In Stock" : "Out of Stock",
}));

export const mockQuotationCart: QuotationCartOrDetail = {
  items: [],
  subtotal: 0,
  vat_percent: 7.5,
  vat_amount: 0,
  total: 0,
};

export const mockStats: DashboardStats = {
  total_assets: mockAssets.reduce((s, a) => s + a.total_quantity, 0),
  available: mockAssets.reduce((s, a) => s + a.available_quantity, 0),
  checked_out: mockAssets.reduce((s, a) => s + a.checked_out_quantity, 0),
  overdue: mockCheckouts.filter((c) => c.status === "overdue").length,
  due_soon: mockCheckouts.filter((c) => {
    const days = (new Date(c.due_at).getTime() - Date.now()) / 86400000;
    return days >= 0 && days <= 2;
  }).length,
  low_stock: mockAssets.filter((a) => a.status === "low" || a.status === "out").length,
  categories: categories.map((c) => ({ name: c, count: mockAssets.filter((a) => a.category === c).reduce((s, a) => s + a.total_quantity, 0) })),
  activity: Array.from({ length: 14 }).map((_, i) => {
    const d = new Date(Date.now() - (13 - i) * 86400000);
    return {
      date: d.toISOString().slice(5, 10),
      checkouts: 2 + Math.round(Math.abs(Math.sin(i * 1.3)) * 8),
      returns: 1 + Math.round(Math.abs(Math.cos(i * 1.1)) * 6),
    };
  }),
};

// Demo data for the Manager/Admin Reporting dashboard (see
// pages/Reports.tsx) -- reuses mockAssets/mockCheckouts so "Demo browsing"
// shows figures consistent with the rest of the app rather than an
// unrelated fabricated dataset. Not a live computation of the real
// services/reports_service.py formulas -- just plausible-looking numbers
// derived from the same fixtures every other mock export already uses.
export const mockReportsDashboard: ReportsDashboard = {
  period: { start_date: null, end_date: null },
  utilization_by_asset_type: mockAssets
    .map((a) => ({
      asset_type_id: a.id,
      name: a.name,
      category: a.category,
      department: a.department,
      total_quantity: a.total_quantity,
      available_quantity: a.available_quantity,
      currently_checked_out: a.checked_out_quantity,
      utilization_rate: a.total_quantity ? Math.round((a.checked_out_quantity / a.total_quantity) * 10000) / 10000 : null,
      checkout_count: mockCheckouts.filter((c) => c.asset_id === a.id).length,
      total_checkout_days: mockCheckouts.filter((c) => c.asset_id === a.id).length * 4.5,
    }))
    .sort((a, b) => (b.utilization_rate ?? 0) - (a.utilization_rate ?? 0)),
  overdue: {
    trend: Array.from({ length: 6 }).map((_, i) => {
      const d = new Date();
      d.setMonth(d.getMonth() - (5 - i));
      return {
        month: d.toISOString().slice(0, 7),
        label: d.toLocaleDateString(undefined, { month: "short", year: "numeric" }),
        overdue_count: i === 5 ? mockCheckouts.filter((c) => c.status === "overdue").length : 1 + (i % 3),
      };
    }),
    total_overdue_now: mockCheckouts.filter((c) => c.status === "overdue").length,
    by_asset_type: mockCheckouts
      .filter((c) => c.status === "overdue")
      .map((c) => ({ name: c.asset_name, overdue_count: 1 })),
    by_department: mockCheckouts
      .filter((c) => c.status === "overdue")
      .map((_, i) => ({ department: categories[i % categories.length], overdue_count: 1 })),
  },
  spend: {
    by_category: categories.map((cat) => {
      const rows = mockAssets.filter((a) => a.category === cat && a.checked_out_quantity > 0);
      return {
        category: cat,
        total_spend: Math.round(rows.reduce((s, a) => s + (a.price ?? 0) * a.checked_out_quantity, 0) * 100) / 100,
        item_count: rows.reduce((s, a) => s + a.checked_out_quantity, 0),
      };
    }).filter((r) => r.item_count > 0).sort((a, b) => b.total_spend - a.total_spend),
    by_department: people.map((p, i) => {
      const rows = mockCheckouts.filter((c) => c.checked_out_to === p);
      const asset = (id: number) => mockAssets.find((a) => a.id === id);
      return {
        department: categories[i % categories.length],
        total_spend: Math.round(rows.reduce((s, c) => s + (asset(c.asset_id)?.price ?? 0) * c.quantity, 0) * 100) / 100,
        item_count: rows.reduce((s, c) => s + c.quantity, 0),
      };
    }).filter((r) => r.item_count > 0).sort((a, b) => b.total_spend - a.total_spend),
    priced_checkout_count: mockCheckouts.length,
    unpriced_checkout_count: 0,
  },
  revenue: (() => {
    const byDepartment = assetDepartments.map((department) => {
      const rows = mockAssets.filter((a) => a.department === department && a.checked_out_quantity > 0);
      const total = rows.reduce((sum, a) => sum + (a.price ?? 0) * a.checked_out_quantity * 4.5, 0);
      return {
        department,
        total_revenue: Math.round(total * 100) / 100,
        item_count: rows.reduce((sum, a) => sum + a.checked_out_quantity, 0),
        quotation_count: rows.length,
      };
    }).filter((r) => r.item_count > 0).sort((a, b) => b.total_revenue - a.total_revenue);
    return {
      by_department: byDepartment,
      total_revenue: Math.round(byDepartment.reduce((sum, r) => sum + r.total_revenue, 0) * 100) / 100,
      fulfilled_quotation_count: byDepartment.reduce((sum, r) => sum + r.quotation_count, 0),
      priced_line_count: byDepartment.length,
      unassigned_line_count: 0,
    };
  })(),
  quotation_turnaround: {
    avg_submit_to_approve_hours: 6.4,
    sample_size_submit_to_approve: 9,
    avg_approve_to_fulfill_hours: 18.2,
    sample_size_approve_to_fulfill: 7,
    avg_submit_to_fulfill_hours: 24.6,
    sample_size_submit_to_fulfill: 7,
    total_quotations_submitted: 11,
    by_month: Array.from({ length: 6 }).map((_, i) => {
      const d = new Date();
      d.setMonth(d.getMonth() - (5 - i));
      return {
        month: d.toISOString().slice(0, 7),
        label: d.toLocaleDateString(undefined, { month: "short", year: "numeric" }),
        avg_submit_to_fulfill_hours: 18 + ((i * 7) % 20),
        sample_size: 1 + (i % 3),
      };
    }),
  },
};
