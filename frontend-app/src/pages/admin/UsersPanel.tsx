// =============================================================================
// User Directory -- ported from js/components/users.js. List/search/custody
// is require_privileged_role; reset-password/delete/restore are
// require_super_admin -- Super Admin AND a plain Admin account alike (see
// deps.py's _FULL_ADMIN_ROLES), so those affordances are hidden only for a
// Manager, who can see the directory itself but not manage accounts in it.
// =============================================================================
import { useEffect, useState } from "react";
import { Plus, KeyRound, Pencil, UserMinus, Trash2 } from "lucide-react";
import { usersApi } from "../../lib/api";
import type { UserRow } from "../../lib/types";
import { isFullAdmin, canManageUserRole } from "../../lib/roles";
import { useCustody } from "../../lib/useCustody";
import { ExportButtons } from "../../components/ExportButtons";
import { PaginationBar, RowsPerPageSelect } from "../../components/PaginationBar";
import { DEFAULT_PAGE_SIZE } from "../../lib/pagination";
import { Modal, ModalHeader } from "../../components/ui/Modal";
import { ErrorBanner } from "../../components/ui/ErrorBanner";
import { SearchInput } from "../../components/ui/SearchInput";
import { TableShell, TableHead, TablePlaceholderRow } from "../../components/ui/TableShell";
import { formInputClass } from "../../components/ui/formStyles";
import { AlertDots } from "./shared";
import { errMsg, ROLE_OPTIONS } from "./sharedHelpers";

function CreateUserModal({ open, onClose, onCreated, roleOptions }: { open: boolean; onClose: () => void; onCreated: () => void; roleOptions: string[] }) {
  const [form, setForm] = useState({ name: "", email: "", phone_number: "", role: roleOptions[0] ?? "staff", password: "", department: "", department_role: "" });
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (open) {
      setForm({ name: "", email: "", phone_number: "", role: roleOptions[0] ?? "staff", password: "", department: "", department_role: "" });
      setError(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  if (!open) return null;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await usersApi.create(form);
      onCreated();
    } catch (err) {
      setError(errMsg(err, "Couldn't create the account."));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal onClose={onClose} size="md" scrollable>
      <ModalHeader title="Provision a system account" onClose={onClose} />
      <form onSubmit={submit} className="flex flex-col gap-3">
        <input required value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="Full name" className={formInputClass} />
        <input required type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} placeholder="Email" className={formInputClass} />
        <input value={form.phone_number} onChange={(e) => setForm({ ...form, phone_number: e.target.value })} placeholder="Phone (optional)" className={formInputClass} />
        <select value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })} className={formInputClass}>
          {roleOptions.map((r) => <option key={r} value={r}>{r}</option>)}
        </select>
        <input value={form.department} onChange={(e) => setForm({ ...form, department: e.target.value })} placeholder="Department (optional)" className={formInputClass} />
        <input value={form.department_role} onChange={(e) => setForm({ ...form, department_role: e.target.value })} placeholder="Title/role in department (optional)" className={formInputClass} />
        <input required type="password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} placeholder="Initial password" className={formInputClass} />
        {error && <ErrorBanner>{error}</ErrorBanner>}
        <button type="submit" disabled={submitting} className="bg-brass hover:bg-brass-soft disabled:opacity-60 text-ink font-medium text-[13px] rounded-[3px] py-2.5 transition-colors">
          {submitting ? "Creating…" : "Create account"}
        </button>
      </form>
    </Modal>
  );
}

function ResetPasswordModal({ target, onClose, onDone }: { target: UserRow | null; onClose: () => void; onDone: () => void }) {
  const [newPassword, setNewPassword] = useState("");
  const [adminPassword, setAdminPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    setNewPassword("");
    setAdminPassword("");
    setError(null);
  }, [target]);

  if (!target) return null;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await usersApi.resetPassword(target.id, newPassword, adminPassword);
      onDone();
    } catch (err) {
      setError(errMsg(err, "Couldn't reset that password."));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal onClose={onClose}>
      <ModalHeader title="Reset password" subtitle={`for ${target.name}`} onClose={onClose} />
      <form onSubmit={submit} className="flex flex-col gap-3">
        <input required type="password" value={newPassword} onChange={(e) => setNewPassword(e.target.value)} placeholder="New password" className={formInputClass} />
        <input required type="password" value={adminPassword} onChange={(e) => setAdminPassword(e.target.value)} placeholder="Your current password" className={formInputClass} />
        {error && <ErrorBanner>{error}</ErrorBanner>}
        <button type="submit" disabled={submitting} className="bg-brass hover:bg-brass-soft disabled:opacity-60 text-ink font-medium text-[13px] rounded-[3px] py-2.5 transition-colors">
          {submitting ? "Resetting…" : "Reset password"}
        </button>
      </form>
    </Modal>
  );
}

function EditUserModal({ target, onClose, onDone }: { target: UserRow | null; onClose: () => void; onDone: () => void }) {
  const [form, setForm] = useState({ name: "", username: "", email: "", phone_number: "", company: "" });
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (target) {
      setForm({ name: target.name, username: target.username ?? "", email: target.email, phone_number: target.phone_number ?? "", company: target.company ?? "" });
      setError(null);
    }
  }, [target]);

  if (!target) return null;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await usersApi.update(target.id, form);
      onDone();
    } catch (err) {
      setError(errMsg(err, "Couldn't save those changes."));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal onClose={onClose}>
      <ModalHeader title="Edit account" onClose={onClose} />
      <form onSubmit={submit} className="flex flex-col gap-3 mt-3">
        <input required value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="Full name" className={formInputClass} />
        <input value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} placeholder="Username (optional)" className={formInputClass} />
        <input required type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} placeholder="Email" className={formInputClass} />
        <input value={form.phone_number} onChange={(e) => setForm({ ...form, phone_number: e.target.value })} placeholder="Phone (optional)" className={formInputClass} />
        <input value={form.company} onChange={(e) => setForm({ ...form, company: e.target.value })} placeholder="Company (optional)" className={formInputClass} />
        {error && <ErrorBanner>{error}</ErrorBanner>}
        <button type="submit" disabled={submitting} className="bg-brass hover:bg-brass-soft disabled:opacity-60 text-ink font-medium text-[13px] rounded-[3px] py-2.5 transition-colors">
          {submitting ? "Saving…" : "Save changes"}
        </button>
      </form>
    </Modal>
  );
}

function RevokeUserModal({ target, onClose, onDone }: { target: UserRow | null; onClose: () => void; onDone: () => void }) {
  const [form, setForm] = useState({ email: "", phone_number: "", company: "" });
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (target) {
      setForm({ email: "", phone_number: "", company: "" });
      setError(null);
    }
  }, [target]);

  if (!target) return null;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!confirm(`Revoke ${target.name}'s login access? They'll become an unlinked Ad-Hoc profile -- their custody history moves with them, but they can no longer sign in.`)) return;
    setSubmitting(true);
    setError(null);
    try {
      const req: Partial<{ email: string; phone_number: string; company: string }> = {};
      if (form.email.trim()) req.email = form.email.trim();
      if (form.phone_number.trim()) req.phone_number = form.phone_number.trim();
      if (form.company.trim()) req.company = form.company.trim();
      await usersApi.convertToOutsider(target.id, req);
      onDone();
    } catch (err) {
      setError(errMsg(err, "Couldn't revoke access."));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal onClose={onClose}>
      <ModalHeader title="Revoke access" subtitle={`Convert ${target.name}'s account into an unlinked Ad-Hoc profile. Fields left blank keep the account's current email/phone.`} onClose={onClose} />
      <form onSubmit={submit} className="flex flex-col gap-3">
        <input type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} placeholder={`Email (default: ${target.email})`} className={formInputClass} />
        <input value={form.phone_number} onChange={(e) => setForm({ ...form, phone_number: e.target.value })} placeholder="Phone (optional)" className={formInputClass} />
        <input value={form.company} onChange={(e) => setForm({ ...form, company: e.target.value })} placeholder="Company (optional)" className={formInputClass} />
        {error && <ErrorBanner>{error}</ErrorBanner>}
        <button type="submit" disabled={submitting} className="bg-rust hover:bg-rust-soft disabled:opacity-60 text-ink font-medium text-[13px] rounded-[3px] py-2.5 transition-colors">
          {submitting ? "Revoking…" : "Revoke access"}
        </button>
      </form>
    </Modal>
  );
}

export function UsersPanel({
  canManage,
  canCreate,
  actorRole,
  demo,
}: {
  /** Super Admin/Admin only -- gates Reset Password and Delete Profile,
   * both require_super_admin on the backend with no Manager exception. */
  canManage: boolean;
  /** Super Admin/Admin (any role), OR a Manager (staff/customer roles
   * only, enforced by CreateUserModal's roleOptions below) -- gates the
   * "New account" button itself. Mirrors POST /users' require_privileged_role. */
  canCreate: boolean;
  actorRole: string | undefined | null;
  demo: boolean;
}) {
  const [rows, setRows] = useState<UserRow[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [perPage, setPerPage] = useState(DEFAULT_PAGE_SIZE);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [resetting, setResetting] = useState<UserRow | null>(null);
  const [editing, setEditing] = useState<UserRow | null>(null);
  const [revoking, setRevoking] = useState<UserRow | null>(null);
  // Custody Ledger drawer is now shared app-wide (see lib/custodyContext.tsx)
  // so the Notification Bell's "View ->" click-through can open it without
  // navigating here first -- this panel's own "Custody" button just opens
  // the same shared drawer instead of owning its own local copy.
  const { openCustody } = useCustody();

  // Edit / Revoke Access per row: full admins act on anyone; a Manager
  // only on a "staff"/"customer" row -- mirrors services/user_service.py's
  // update_user()/convert_user_to_outsider() MANAGER_PROVISIONABLE_ROLES
  // check, so a Manager is never shown a button that would just come
  // back as a 403.
  const canEditRow = (targetRole: string) => canManageUserRole(actorRole, targetRole, demo);

  // Managers may only ever provision a "staff" or "customer" account --
  // mirrors manager.html's Provision form, which never even offers a
  // Manager/Admin option, and services/user_service.py's own enforcement
  // of the same limit.
  const createRoleOptions = demo || isFullAdmin(actorRole) ? ROLE_OPTIONS : ["staff", "customer"];

  const refresh = () => {
    setLoading(true);
    usersApi.list(perPage, offset, search).then((res) => {
      setRows(res.items);
      setTotal(res.total);
      setLoading(false);
    });
  };

  useEffect(refresh, [offset, perPage, search]);

  const handlePerPageChange = (n: number) => {
    setPerPage(n);
    setOffset(0);
  };

  const remove = async (u: UserRow) => {
    if (!confirm(`Delete ${u.name}'s account? It can be restored later from the Deleted list.`)) return;
    await usersApi.remove(u.id);
    refresh();
  };

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-2 flex-wrap">
        <SearchInput value={search} onChange={(v) => { setOffset(0); setSearch(v); }} placeholder="Search directory…" className="relative flex-1 max-w-xs" />
        <div className="flex items-center gap-2 flex-wrap sm:ml-auto">
          <RowsPerPageSelect value={perPage} onChange={handlePerPageChange} />
          <ExportButtons
            compact
            disabled={total === 0}
            urlFor={(format) => usersApi.exportUrl(format)}
            filenameFor={(format) => `all_users_properties.${format}`}
          />
          {canCreate && (
            <button onClick={() => setCreating(true)} className="flex items-center gap-1.5 bg-brass hover:bg-brass-soft text-ink font-medium text-[12px] rounded-[3px] px-3 py-2 transition-colors">
              <Plus size={12} /> New account
            </button>
          )}
        </div>
      </div>

      <TableShell>
        <table className="w-full text-left text-[12.5px]">
          <TableHead>
            <th className="px-5 py-3 font-medium">Name</th>
            <th className="hidden sm:table-cell px-5 py-3 font-medium">Role</th>
            <th className="hidden sm:table-cell px-5 py-3 font-medium">Custody</th>
            <th className="px-5 py-3 font-medium text-right">Actions</th>
          </TableHead>
          <tbody className="divide-y divide-border-soft">
            {loading && <TablePlaceholderRow columns={4}>Loading…</TablePlaceholderRow>}
            {!loading && rows.length === 0 && <TablePlaceholderRow columns={4}>No accounts match.</TablePlaceholderRow>}
            {rows.map((u) => (
              <tr key={u.id}>
                <td className="px-5 py-3">
                  <p className="text-text font-medium flex items-center">{u.name}<AlertDots alerts={u.alerts} /></p>
                  <p className="text-[11px] text-text-faint">{u.email}</p>
                </td>
                <td className="hidden sm:table-cell px-5 py-3 text-text-muted capitalize">{u.role.replace("_", " ")}{u.department_role ? ` · ${u.department_role}` : ""}</td>
                <td className="hidden sm:table-cell px-5 py-3 font-mono text-text-muted">{u.checkout_count}</td>
                <td className="px-5 py-3 text-right">
                  <div className="flex items-center justify-end gap-1.5 flex-wrap">
                    <button onClick={() => openCustody("user", u.id, u.name)} className="rounded-md border border-border-soft px-2 py-1 text-[11px] font-medium text-text-muted hover:border-sky/50 hover:text-sky transition-colors">Custody</button>
                    {canEditRow(u.role) && <button onClick={() => setEditing(u)} title="Edit" className="rounded-md border border-border-soft px-2 py-1 text-[11px] font-medium text-text-muted hover:border-brass/50 hover:text-brass-soft transition-colors"><Pencil size={11} /></button>}
                    {canManage && <button onClick={() => setResetting(u)} title="Reset password" className="rounded-md border border-border-soft px-2 py-1 text-[11px] font-medium text-text-muted hover:border-brass/50 hover:text-brass-soft transition-colors"><KeyRound size={11} /></button>}
                    {canEditRow(u.role) && <button onClick={() => setRevoking(u)} title="Revoke access (convert to Ad-Hoc)" className="rounded-md border border-border-soft px-2 py-1 text-[11px] font-medium text-text-muted hover:border-rust/50 hover:text-rust-soft transition-colors"><UserMinus size={11} /></button>}
                    {canManage && <button onClick={() => remove(u)} title="Delete" className="rounded-md border border-border-soft px-2 py-1 text-[11px] font-medium text-text-muted hover:border-rust/50 hover:text-rust-soft transition-colors"><Trash2 size={11} /></button>}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </TableShell>

      <PaginationBar total={total} perPage={perPage} offset={offset} onOffsetChange={setOffset} />

      <CreateUserModal open={creating} onClose={() => setCreating(false)} onCreated={() => { setCreating(false); refresh(); }} roleOptions={createRoleOptions} />
      <ResetPasswordModal target={resetting} onClose={() => setResetting(null)} onDone={() => { setResetting(null); alert("Password reset."); }} />
      <EditUserModal target={editing} onClose={() => setEditing(null)} onDone={() => { setEditing(null); refresh(); }} />
      <RevokeUserModal target={revoking} onClose={() => setRevoking(null)} onDone={() => { setRevoking(null); refresh(); }} />
    </div>
  );
}
