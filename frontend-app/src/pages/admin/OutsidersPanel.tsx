// =============================================================================
// Ad-Hoc (Unlinked) Directory -- ported from js/components/outsiders.js.
// External individuals dispatched equipment without ever holding a login.
// =============================================================================
import { useEffect, useState } from "react";
import { Pencil, ArrowRightLeft, Trash2 } from "lucide-react";
import { outsidersApi } from "../../lib/api";
import type { OutsiderRow } from "../../lib/types";
import { isFullAdmin } from "../../lib/roles";
import { useCustody } from "../../lib/custodyContext";
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

function EditOutsiderModal({ target, onClose, onDone }: { target: OutsiderRow | null; onClose: () => void; onDone: () => void }) {
  const [form, setForm] = useState({ name: "", email: "", phone_number: "", company: "" });
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (target) {
      setForm({ name: target.name, email: target.email ?? "", phone_number: target.phone_number ?? "", company: target.company ?? "" });
      setError(null);
    }
  }, [target]);

  if (!target) return null;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await outsidersApi.update(target.id, form);
      onDone();
    } catch (err) {
      setError(errMsg(err, "Couldn't save those changes."));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal onClose={onClose}>
      <ModalHeader title="Edit ad-hoc profile" onClose={onClose} />
      <form onSubmit={submit} className="flex flex-col gap-3 mt-3">
        <input required value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="Full name" className={formInputClass} />
        <input type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} placeholder="Email (optional)" className={formInputClass} />
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

function ConvertOutsiderModal({ target, onClose, onDone, roleOptions }: { target: OutsiderRow | null; onClose: () => void; onDone: () => void; roleOptions: string[] }) {
  const [form, setForm] = useState({ email: "", phone_number: "", password: "", role: roleOptions[0] ?? "staff", department: "", department_role: "" });
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (target) {
      setForm({ email: target.email ?? "", phone_number: target.phone_number ?? "", password: "", role: roleOptions[0] ?? "staff", department: "", department_role: "" });
      setError(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target]);

  if (!target) return null;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await outsidersApi.convertToUser(target.id, form);
      onDone();
    } catch (err) {
      setError(errMsg(err, "Couldn't convert this profile."));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal onClose={onClose} scrollable>
      <ModalHeader title="Convert to user" subtitle={`Give ${target.name} a real login account. Their name carries over from this profile.`} onClose={onClose} />
      <form onSubmit={submit} className="flex flex-col gap-3">
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
          {submitting ? "Converting…" : "Convert to user"}
        </button>
      </form>
    </Modal>
  );
}

export function OutsidersPanel({
  canManage,
  actorRole,
  demo,
}: {
  canManage: boolean;
  actorRole: string | undefined | null;
  demo: boolean;
}) {
  const [rows, setRows] = useState<OutsiderRow[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [perPage, setPerPage] = useState(DEFAULT_PAGE_SIZE);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState<OutsiderRow | null>(null);
  const [converting, setConverting] = useState<OutsiderRow | null>(null);
  // Custody Ledger drawer is shared app-wide -- see lib/custodyContext.tsx
  // and UsersPanel.tsx's matching comment.
  const { openCustody } = useCustody();

  // Same Manager role ceiling as UsersPanel's createRoleOptions above --
  // mirrors manager.html's "Convert to user" role select and
  // services/outsider_service.py's convert_outsider_to_user().
  const convertRoleOptions = demo || isFullAdmin(actorRole) ? ROLE_OPTIONS : ["staff", "customer"];

  const refresh = () => {
    setLoading(true);
    outsidersApi.list(perPage, offset, search).then((res) => {
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

  const remove = async (o: OutsiderRow) => {
    if (!confirm(`Delete ${o.name}'s ad-hoc profile? Their historical checkout records are kept.`)) return;
    await outsidersApi.remove(o.id);
    refresh();
  };

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-2 flex-wrap">
        <SearchInput value={search} onChange={(v) => { setOffset(0); setSearch(v); }} placeholder="Search ad-hoc profiles…" />
        <RowsPerPageSelect value={perPage} onChange={handlePerPageChange} />
        <ExportButtons
          compact
          disabled={total === 0}
          urlFor={(format) => outsidersApi.exportUrl(format)}
          filenameFor={(format) => `all_outsiders_properties.${format}`}
        />
      </div>

      <TableShell>
        <table className="w-full text-left text-[12.5px]">
          <TableHead>
            <th className="px-5 py-3 font-medium">Name</th>
            <th className="hidden sm:table-cell px-5 py-3 font-medium">Company</th>
            <th className="hidden sm:table-cell px-5 py-3 font-medium">Custody</th>
            <th className="px-5 py-3 font-medium text-right">Actions</th>
          </TableHead>
          <tbody className="divide-y divide-border-soft">
            {loading && <TablePlaceholderRow columns={4}>Loading…</TablePlaceholderRow>}
            {!loading && rows.length === 0 && <TablePlaceholderRow columns={4}>No ad-hoc profiles match.</TablePlaceholderRow>}
            {rows.map((o) => (
              <tr key={o.id}>
                <td className="px-5 py-3">
                  <p className="text-text font-medium flex items-center">{o.name}<AlertDots alerts={o.alerts} /></p>
                  <p className="text-[11px] text-text-faint">{o.email || "—"}</p>
                </td>
                <td className="hidden sm:table-cell px-5 py-3 text-text-muted">{o.company || "—"}</td>
                <td className="hidden sm:table-cell px-5 py-3 font-mono text-text-muted">{o.outstanding_items}</td>
                <td className="px-5 py-3 text-right">
                  <div className="flex items-center justify-end gap-1.5">
                    <button onClick={() => openCustody("outsider", o.id, o.name)} className="rounded-md border border-border-soft px-2 py-1 text-[11px] font-medium text-text-muted hover:border-sky/50 hover:text-sky transition-colors">Custody</button>
                    {canManage && <button onClick={() => setEditing(o)} title="Edit" className="rounded-md border border-border-soft px-2 py-1 text-[11px] font-medium text-text-muted hover:border-brass/50 hover:text-brass-soft transition-colors"><Pencil size={11} /></button>}
                    {canManage && <button onClick={() => setConverting(o)} title="Convert to user" className="rounded-md border border-border-soft px-2 py-1 text-[11px] font-medium text-text-muted hover:border-moss/50 hover:text-moss-soft transition-colors"><ArrowRightLeft size={11} /></button>}
                    {canManage && <button onClick={() => remove(o)} title="Delete" className="rounded-md border border-border-soft px-2 py-1 text-[11px] font-medium text-text-muted hover:border-rust/50 hover:text-rust-soft transition-colors"><Trash2 size={11} /></button>}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </TableShell>

      <PaginationBar total={total} perPage={perPage} offset={offset} onOffsetChange={setOffset} />

      <EditOutsiderModal target={editing} onClose={() => setEditing(null)} onDone={() => { setEditing(null); refresh(); }} />
      <ConvertOutsiderModal target={converting} onClose={() => setConverting(null)} onDone={() => { setConverting(null); refresh(); }} roleOptions={convertRoleOptions} />
    </div>
  );
}
