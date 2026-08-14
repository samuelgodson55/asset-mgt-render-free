// Mirrors backend/deps.py's role gates so the UI hides/shows admin
// affordances consistent with what the backend would actually allow --
// this is a UX convenience only, never the real enforcement (the backend
// re-checks every request regardless of what the UI shows).

// require_super_admin: Super Admin OR a plain Admin account (treated as
// fully equivalent almost everywhere in this app).
const FULL_ADMIN_ROLES = new Set(["admin", "super_admin"]);

export function isFullAdmin(role: string | undefined | null): boolean {
  return !!role && FULL_ADMIN_ROLES.has(role);
}

// require_true_super_admin: the root Super Admin account ONLY -- a plain
// `admin` is deliberately excluded here (used for /backup/* -- a backup
// contains literally everything, including other admins' credentials).
export function isTrueSuperAdmin(role: string | undefined | null): boolean {
  return role === "super_admin";
}

// require_privileged_role: Super Admin, Admin, OR Manager -- used for
// custody-affecting actions like extension-request review/decision and
// the overdue/due-soon system-wide feeds.
const PRIVILEGED_ROLES = new Set(["admin", "super_admin", "manager"]);

export function isPrivileged(role: string | undefined | null): boolean {
  return !!role && PRIVILEGED_ROLES.has(role);
}

// Mirrors services/user_service.py's MANAGER_PROVISIONABLE_ROLES: a
// Manager may create/edit/revoke only Staff/Customer accounts and may only
// switch their role within that Staff/Customer boundary. Admin/Super Admin
// can manage any non-root account and assign Manager/Admin privileges. A Super Admin/Admin
// has no such restriction and can act on any account (see deps.py's
// require_privileged_role for the create/edit/revoke routes themselves,
// and update_user()/convert_user_to_outsider()'s own extra per-row check
// on top of that for the Manager case specifically).
const MANAGER_PROVISIONABLE_ROLES = new Set(["staff", "customer"]);

export function isManagerProvisionableRole(role: string | undefined | null): boolean {
  return !!role && MANAGER_PROVISIONABLE_ROLES.has(role);
}

// Whether `actorRole` may create/edit/revoke-login-for an account whose
// OWN role is `targetRole` (irrelevant for create, where there's no
// existing row yet -- pass the role about to be assigned instead). Full
// admins (Super Admin/Admin) always can; a Manager only when the role in
// question is staff/customer.
export function canManageUserRole(
  actorRole: string | undefined | null,
  targetRole: string | undefined | null,
  demo: boolean
): boolean {
  if (demo || isFullAdmin(actorRole)) return true;
  return actorRole === "manager" && isManagerProvisionableRole(targetRole);
}

// Mirrors services/quotation_service.py's list_catalog(): a Manager/Admin/
// Super Admin sees real-time stock (available_quantity/status) no matter
// what; a Staff/Customer sees it only when the operator has explicitly
// turned CATALOG_SHOW_STOCK_TO_STAFF_CUSTOMER on. `catalogShowStock` is
// that flag's current value, read once from GET /config/public (see
// lib/auth.tsx). Used to gate every stock-derived UI element -- Dashboard's
// Available/Low-stock cards, the Inventory tag grid, and the Asset Drawer --
// not just the Quotation Catalog it was originally scoped to, since the
// setting is meant to answer one question ("can this role see live stock
// numbers?") everywhere in the app, not just on one page.
export function canSeeStock(
  role: string | undefined | null,
  demo: boolean,
  catalogShowStock: boolean
): boolean {
  return demo || isPrivileged(role) || catalogShowStock;
}
