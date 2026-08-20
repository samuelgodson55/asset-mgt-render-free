// =============================================================================
// Admin & Manager pages -- these two roles share one page shell
// (AdminOrManagerPage below), which just renders a set of tabbed panels
// gated by role. The panels themselves live under ./admin/* so each one is
// independently readable/debuggable rather than living in one 2400-line
// file -- see src/pages/admin/shared.tsx for the small set of helpers
// shared across more than one panel.
// =============================================================================
import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { motion } from "framer-motion";
import {
  FileSpreadsheet,
  ShieldAlert,
  Users as UsersIcon,
  Contact,
  ScrollText,
  FileText,
  Boxes,
  Percent,
  UserMinus,
  DatabaseBackup,
  Wrench,
  Activity,
} from "lucide-react";
import { useAuth } from "../lib/useAuth";
import { isFullAdmin, isTrueSuperAdmin, isPrivileged } from "../lib/roles";
import { InventoryImportPanel } from "./admin/InventoryImportPanel";
import { SystemBackupsPanel } from "./admin/SystemBackupsPanel";
import { SettingsPanel } from "./admin/SettingsPanel";
import { UsersPanel } from "./admin/UsersPanel";
import { OutsidersPanel } from "./admin/OutsidersPanel";
import { DeletedUsersPanel } from "./admin/DeletedUsersPanel";
import { DeletedAssetsPanel } from "./admin/DeletedAssetsPanel";
import { AuditPanel } from "./admin/AuditPanel";
import { QuotesPanel } from "./admin/QuotesPanel";
import { MaintenanceModePanel } from "./admin/MaintenanceModePanel";
import { DatabaseHealthPanel } from "./admin/DatabaseHealthPanel";

// =============================================================================
// Admin / Manager -- two separate pages/routes (/admin, /manager) sharing
// one implementation, mirroring the legacy frontend's admin.html vs
// manager.html: same underlying panels, but their own URL, header, and
// mode pill rather than one page whose contents silently reshape around
// whoever happens to be signed in. Which route a role can even reach is
// enforced up in App.tsx's RequireRole; the tab visibility below is the
// second, finer-grained layer -- see lib/roles.ts.
// =============================================================================

export function Admin() {
  return <AdminOrManagerPage variant="admin" />;
}

export function Manager() {
  return <AdminOrManagerPage variant="manager" />;
}

function AdminOrManagerPage({ variant }: { variant: "admin" | "manager" }) {
  const { user, demo } = useAuth();
  const isManager = variant === "manager";

  // The Custody Ledger drawer (Notifications' "View ->" click-through, and
  // each panel's own "Custody" button) is owned above this page entirely --
  // see lib/custodyContext.tsx -- so opening it no longer needs a
  // ?custody= deep-link query param, tab-forcing, or any state here at all.
  // The Manager page never offers Inventory Import or System Backups --
  // same as manager.html never having those sections at all, regardless
  // of who's actually viewing it -- while the Admin page keeps gating
  // them on the visitor's real role exactly as before.
  const canImport = !isManager && (demo || isFullAdmin(user?.role));
  // Backups stay Super-Admin-only (deps.require_true_super_admin) -- a
  // backup contains literally everything, including every `admin`
  // account's own credentials, so letting an `admin` view/restore one
  // would let that action expose or tamper with the very accounts meant
  // to be holding it accountable.
  const canBackups = !isManager && (demo || isTrueSuperAdmin(user?.role));
  // PUT /maintenance/status is require_true_super_admin too -- kept as its
  // own flag (rather than reusing canBackups, which happens to share the
  // exact same gate) for the same reason canSettings is kept separate from
  // canBackups below: a name tied to the feature it guards, not to
  // whichever other feature happened to share its backend role check.
  const canMaintenance = !isManager && (demo || isTrueSuperAdmin(user?.role));
  const canDatabaseHealth = !isManager && (demo || isTrueSuperAdmin(user?.role));
  const canDirectory = demo || isPrivileged(user?.role);
  // reset-password/delete/restore/purge (deps.require_super_admin) treat
  // Admin and Super Admin identically on the backend -- mirrored here as
  // isFullAdmin rather than isTrueSuperAdmin so a plain Admin account
  // actually gets those affordances the backend already grants it.
  const canManageAccounts = demo || isFullAdmin(user?.role);
  // Create/Edit/Revoke are also open to a Manager -- POST /users, PUT
  // /users/{id}, and POST /users/{id}/convert-to-outsider are all
  // require_privileged_role, not require_super_admin (only reset-
  // password/delete/restore/purge above are Super-Admin/Admin-only). A
  // Manager is further limited to staff/customer accounts specifically
  // (see lib/roles.ts's canManageUserRole()), enforced per-row inside
  // UsersPanel/OutsidersPanel rather than by hiding the whole tab.
  const canCreateAccounts = demo || isFullAdmin(user?.role) || user?.role === "manager";
  // Same require_super_admin gate as GET /assets/deleted and
  // POST /assets/{id}/restore -- Admin, not just the root Super Admin.
  const canDeletedAssets = !isManager && (demo || isFullAdmin(user?.role));
  // PUT /settings/vat is require_super_admin too -- kept as its own flag
  // (rather than reusing canBackups) since it's a different backend gate
  // that just happens to share a tab group with System Backups.
  const canSettings = !isManager && (demo || isFullAdmin(user?.role));

  type Tab = { key: "import" | "backups" | "maintenance" | "database-health" | "users" | "outsiders" | "audit" | "quotes" | "deleted-assets" | "deleted-users" | "settings"; label: string; icon: typeof FileSpreadsheet };
  const tabs = useMemo<Tab[]>(() => {
    const list: Tab[] = [];
    if (canDirectory) list.push({ key: "users", label: "User Directory", icon: UsersIcon });
    if (canDirectory) list.push({ key: "outsiders", label: "Ad-Hoc Directory", icon: Contact });
    if (canDirectory) list.push({ key: "quotes", label: "Quotes", icon: FileText });
    if (canDirectory) list.push({ key: "audit", label: "Audit Trail", icon: ScrollText });
    if (canImport) list.push({ key: "import", label: "Inventory Import", icon: FileSpreadsheet });
    if (canDeletedAssets) list.push({ key: "deleted-assets", label: "Deleted Assets", icon: Boxes });
    if (canDeletedAssets) list.push({ key: "deleted-users", label: "Deleted Users", icon: UserMinus });
    if (canBackups) list.push({ key: "backups", label: "System Backups", icon: DatabaseBackup });
    if (canMaintenance) list.push({ key: "maintenance", label: "Maintenance Mode", icon: Wrench });
    if (canDatabaseHealth) list.push({ key: "database-health", label: "Database Health", icon: Activity });
    if (canSettings) list.push({ key: "settings", label: "Settings", icon: Percent });
    return list;
  }, [canImport, canBackups, canMaintenance, canDatabaseHealth, canDirectory, canDeletedAssets, canSettings]);

  const [tab, setTab] = useState<Tab["key"] | null>(tabs[0]?.key ?? null);
  useEffect(() => {
    if (!tabs.some((t) => t.key === tab)) setTab(tabs[0]?.key ?? null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tabs]);

  // Deep-link support (?tab=<key>) -- lets the global header search (see
  // Layout.tsx's submitHeaderSearch()) land a privileged session straight
  // on the Quotes tab for a quotation that isn't the searcher's own (the
  // ?openQuote= param QuotesPanel.tsx reads is only useful once that tab
  // is actually the one showing). Runs once tabs is populated so it isn't
  // racing the role-gated tab list above; stripped right after so a
  // manual tab click or page refresh isn't pinned to it forever.
  const [searchParams, setSearchParams] = useSearchParams();
  useEffect(() => {
    if (tabs.length === 0) return;
    const requested = searchParams.get("tab");
    if (!requested) return;
    if (tabs.some((t) => t.key === requested)) setTab(requested as Tab["key"]);
    setSearchParams((prev) => { const next = new URLSearchParams(prev); next.delete("tab"); return next; }, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tabs]);

  return (
    <div>
      <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }} className="mb-6 flex items-start justify-between gap-3 flex-wrap">
        <div>
          <h1 className="font-display text-2xl font-semibold text-text">{isManager ? "Manager" : "Admin"}</h1>
          <p className="text-text-muted text-sm mt-1">
            {isManager
              ? "Directories, quotes, and the audit trail — scoped to what a Manager can see and do."
              : "Directories, audit trail, inventory import, system health, and system-level backup controls."}
          </p>
        </div>
        <span
          className={`hidden sm:flex items-center gap-1.5 rounded-full border px-3 py-1 text-[11px] font-semibold uppercase tracking-wide ${
            isManager ? "border-brass/50 bg-brass/10 text-brass-soft" : "border-rust/50 bg-rust/10 text-rust-soft"
          }`}
        >
          <span className={`h-1.5 w-1.5 rounded-full ${isManager ? "bg-brass" : "bg-rust"} animate-pulse`} />
          {isManager ? "Manager Mode" : "Admin Mode"}
        </span>
      </motion.div>

      {tabs.length === 0 ? (
        <div className="flex flex-col items-center justify-center text-center py-20 border border-border-soft rounded-[3px] bg-surface">
          <ShieldAlert size={20} className="text-text-faint mb-3" />
          <p className="text-[13px] text-text-muted max-w-sm">
            Your role doesn't include access to anything in {isManager ? "Manager" : "Admin"}. Ask a Super Admin if you need something here.
          </p>
        </div>
      ) : (
        <>
          <div className="flex items-center gap-2 mb-5 flex-wrap">
            {tabs.map((t) => (
              <button
                key={t.key}
                onClick={() => setTab(t.key)}
                className={`flex items-center gap-1.5 px-3 py-1.5 rounded-full text-[12px] font-medium border transition-colors ${
                  tab === t.key ? "bg-brass/15 border-brass/40 text-brass-soft" : "border-border-soft text-text-muted hover:text-text hover:border-border"
                }`}
              >
                <t.icon size={12} /> {t.label}
              </button>
            ))}
          </div>

          {tab === "users" && canDirectory && (
            <UsersPanel
              canManage={canManageAccounts}
              canCreate={canCreateAccounts}
              actorRole={user?.role}
              demo={demo}
            />
          )}
          {tab === "outsiders" && canDirectory && (
            <OutsidersPanel
              canManage={canDirectory}
              actorRole={user?.role}
              demo={demo}
            />
          )}
          {tab === "quotes" && canDirectory && <QuotesPanel />}
          {tab === "audit" && canDirectory && <AuditPanel />}
          {tab === "import" && canImport && <InventoryImportPanel />}
          {tab === "deleted-assets" && canDeletedAssets && <DeletedAssetsPanel />}
          {tab === "deleted-users" && canDeletedAssets && <DeletedUsersPanel />}
          {tab === "backups" && canBackups && <SystemBackupsPanel />}
          {tab === "maintenance" && canMaintenance && <MaintenanceModePanel />}
          {tab === "database-health" && canDatabaseHealth && <DatabaseHealthPanel />}
          {tab === "settings" && canSettings && <SettingsPanel />}
        </>
      )}
    </div>
  );
}
