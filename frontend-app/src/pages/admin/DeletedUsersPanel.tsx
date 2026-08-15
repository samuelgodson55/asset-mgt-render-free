// =============================================================================
// Restore Deleted Users -- ported from js/components/users.js's
// loadDeletedUsers()/restoreUser(). require_super_admin on GET /users/deleted
// and POST /users/{id}/restore -- Super Admin AND a plain Admin account,
// Admin page only -- never shown on the Manager page, same tier as
// System Backups (which, unlike this, stays Super-Admin-only).
// =============================================================================
import { useEffect, useState } from "react";
import { usersApi } from "../../lib/api";
import type { DeletedUserRow } from "../../lib/types";
import { PaginationBar, RowsPerPageSelect } from "../../components/PaginationBar";
import { DEFAULT_PAGE_SIZE } from "../../lib/pagination";
import { useRequestGuard } from "../../lib/useRequestGuard";
import { ErrorBanner } from "../../components/ui/ErrorBanner";
import { SearchInput } from "../../components/ui/SearchInput";
import { TableShell, TableHead, TablePlaceholderRow } from "../../components/ui/TableShell";
import { errMsg, formatWhen } from "./sharedHelpers";

export function DeletedUsersPanel() {
  const [rows, setRows] = useState<DeletedUserRow[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [perPage, setPerPage] = useState(DEFAULT_PAGE_SIZE);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const beginRequest = useRequestGuard();

  const refresh = () => {
    const isCurrent = beginRequest();
    setLoading(true);
    setError(null);
    usersApi.listDeleted(perPage, offset, search)
      .then((res) => { if (isCurrent()) { setRows(res.items); setTotal(res.total); } })
      .catch((err) => { if (isCurrent()) setError(errMsg(err, "Couldn't load deleted accounts.")); })
      .finally(() => { if (isCurrent()) setLoading(false); });
  };

  useEffect(refresh, [offset, perPage, search]);

  useEffect(() => {
    if (offset > 0 && offset >= total) setOffset(Math.max(0, Math.floor(Math.max(total - 1, 0) / perPage) * perPage));
  }, [offset, total, perPage]);

  const handlePerPageChange = (n: number) => {
    setPerPage(n);
    setOffset(0);
  };

  const restore = async (u: DeletedUserRow) => {
    if (!confirm(`Restore ${u.name}'s account? Their login will work again and they'll reappear in the User Directory immediately.`)) return;
    setBusyId(u.id);
    try {
      await usersApi.restore(u.id);
      refresh();
    } catch (err) {
      setError(errMsg(err, "Restore failed."));
    } finally {
      setBusyId(null);
    }
  };

  const purge = async (u: DeletedUserRow) => {
    if (!confirm(`Permanently purge ${u.name}'s account? This cannot be undone. Their email/username will be freed up for reuse, but this account can no longer be restored afterward.`)) return;
    setBusyId(u.id);
    try {
      await usersApi.purge(u.id);
      refresh();
    } catch (err) {
      setError(errMsg(err, "Purge failed."));
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-2 flex-wrap">
        <SearchInput value={search} onChange={(v) => { setOffset(0); setSearch(v); }} placeholder="Search deleted accounts…" />
        <RowsPerPageSelect value={perPage} onChange={handlePerPageChange} />
      </div>

      {error && <ErrorBanner>{error}</ErrorBanner>}

      <TableShell>
        <table className="w-full text-left text-[12.5px]">
          <TableHead>
            <th className="px-5 py-3 font-medium">Name</th>
            <th className="hidden sm:table-cell px-5 py-3 font-medium">Role</th>
            <th className="hidden sm:table-cell px-5 py-3 font-medium">Deleted on</th>
            <th className="px-5 py-3 font-medium text-right">Actions</th>
          </TableHead>
          <tbody className="divide-y divide-border-soft">
            {loading && <TablePlaceholderRow columns={4}>Loading…</TablePlaceholderRow>}
            {!loading && rows.length === 0 && <TablePlaceholderRow columns={4}>No deleted accounts.</TablePlaceholderRow>}
            {rows.map((u) => (
              <tr key={u.id}>
                <td className="px-5 py-3">
                  <p className="text-text font-medium">{u.name}</p>
                  <p className="text-[11px] text-text-faint">{u.email}</p>
                </td>
                <td className="hidden sm:table-cell px-5 py-3 text-text-muted capitalize">{u.role.replace("_", " ")}</td>
                <td className="hidden sm:table-cell px-5 py-3 text-text-muted">{u.deleted_at ? formatWhen(u.deleted_at) : "—"}</td>
                <td className="px-5 py-3 text-right">
                  <div className="flex items-center justify-end gap-1.5 flex-wrap">
                    <button onClick={() => restore(u)} disabled={busyId === u.id} className="rounded-md border border-moss/40 px-2 py-1 text-[11px] font-medium text-moss-soft hover:bg-moss/10 disabled:opacity-50 transition-colors">Restore</button>
                    <button onClick={() => purge(u)} disabled={busyId === u.id} className="rounded-md border border-rust/40 px-2 py-1 text-[11px] font-medium text-rust-soft hover:bg-rust/10 disabled:opacity-50 transition-colors">Purge</button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </TableShell>

      <PaginationBar total={total} perPage={perPage} offset={offset} onOffsetChange={setOffset} />
    </div>
  );
}
