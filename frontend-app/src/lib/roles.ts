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
