import type { AssetType, Checkout, ExtensionRequest, NotificationItem, DashboardStats, BackupEntry, BackupStatus, CatalogAsset, QuotationCartOrDetail } from "./types";

const categories = ["Field Radios", "Optics", "Power", "Networking", "Fabrication", "Safety"];

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

// Demo data for the self-service Quotation Catalog/cart (see
// pages/Quotations.tsx) -- reuses mockAssets' own names/prices/stock so
// "Demo browsing" shows the same inventory everywhere in the app.
export const mockCatalog: CatalogAsset[] = mockAssets.map((a) => ({
  id: a.id,
  name: a.name,
  category: a.category,
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
