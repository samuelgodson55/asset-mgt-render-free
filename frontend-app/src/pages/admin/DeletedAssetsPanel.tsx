// =============================================================================
// Restore Deleted Assets -- ported from js/components/assets.js's
// loadDeletedAssets()/restoreAssetPool()/purgeAssetPool(). require_super_admin
// on GET /assets/deleted, POST /assets/{id}/restore, POST /assets/{id}/purge
// -- Super Admin AND a plain Admin account, same as the main Asset Inventory
// table's manage actions; still not shown to Manager.
// =============================================================================
import { useEffect, useState } from "react";
import { assetsApi } from "../../lib/api";
import type { DeletedAssetRow } from "../../lib/types";
import { PaginationBar, RowsPerPageSelect } from "../../components/PaginationBar";
import { DEFAULT_PAGE_SIZE } from "../../lib/pagination";
import { ErrorBanner } from "../../components/ui/ErrorBanner";
import { SearchInput } from "../../components/ui/SearchInput";
import { TableShell, TableHead, TablePlaceholderRow } from "../../components/ui/TableShell";
import { errMsg, formatWhen } from "./sharedHelpers";

export function DeletedAssetsPanel() {
  const [rows, setRows] = useState<DeletedAssetRow[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [perPage, setPerPage] = useState(DEFAULT_PAGE_SIZE);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = () => {
    setLoading(true);
    setError(null);
    assetsApi
      .listDeleted(perPage, offset, search)
      .then((res) => {
        setRows(res.items);
        setTotal(res.total);
      })
      .catch((err) => setError(errMsg(err, "Couldn't load deleted asset pools.")))
      .finally(() => setLoading(false));
  };

  useEffect(refresh, [offset, perPage, search]);

  const handlePerPageChange = (n: number) => {
    setPerPage(n);
    setOffset(0);
  };

  const restore = async (a: DeletedAssetRow) => {
    if (!confirm(`Restore asset pool "${a.name}"? It will reappear in the active Asset Inventory table immediately.`)) return;
    setBusyId(a.id);
    try {
      await assetsApi.restore(a.id);
      refresh();
    } catch (err) {
      setError(errMsg(err, "Restore failed."));
    } finally {
      setBusyId(null);
    }
  };

  const purge = async (a: DeletedAssetRow) => {
    if (!confirm(`Permanently purge asset pool "${a.name}"? This cannot be undone. Its name will be freed up for reuse by a new pool, but this pool can no longer be restored afterward.`)) return;
    setBusyId(a.id);
    try {
      await assetsApi.purge(a.id);
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
        <SearchInput value={search} onChange={(v) => { setOffset(0); setSearch(v); }} placeholder="Search deleted pools…" />
        <RowsPerPageSelect value={perPage} onChange={handlePerPageChange} />
      </div>

      {error && <ErrorBanner>{error}</ErrorBanner>}

      <TableShell>
        <table className="w-full text-left text-[12.5px]">
          <TableHead>
            <th className="px-5 py-3 font-medium">Name</th>
            <th className="hidden sm:table-cell px-5 py-3 font-medium">Total qty</th>
            <th className="hidden sm:table-cell px-5 py-3 font-medium">Deleted on</th>
            <th className="px-5 py-3 font-medium text-right">Actions</th>
          </TableHead>
          <tbody className="divide-y divide-border-soft">
            {loading && <TablePlaceholderRow columns={4}>Loading…</TablePlaceholderRow>}
            {!loading && rows.length === 0 && <TablePlaceholderRow columns={4}>No deleted asset pools.</TablePlaceholderRow>}
            {rows.map((a) => (
              <tr key={a.id}>
                <td className="px-5 py-3">
                  <p className="text-text font-medium">{a.name}</p>
                  <p className="text-[11px] text-text-faint font-mono">POOL-{String(a.id).padStart(4, "0")}{a.category ? ` · ${a.category}` : ""}</p>
                </td>
                <td className="hidden sm:table-cell px-5 py-3 font-mono text-text-muted">{a.total_quantity}</td>
                <td className="hidden sm:table-cell px-5 py-3 text-text-muted">{a.deleted_at ? formatWhen(a.deleted_at) : "—"}</td>
                <td className="px-5 py-3 text-right">
                  <div className="flex items-center justify-end gap-1.5 flex-wrap">
                    <button onClick={() => restore(a)} disabled={busyId === a.id} className="rounded-md border border-moss/40 px-2 py-1 text-[11px] font-medium text-moss-soft hover:bg-moss/10 disabled:opacity-50 transition-colors">Restore</button>
                    <button onClick={() => purge(a)} disabled={busyId === a.id} className="rounded-md border border-rust/40 px-2 py-1 text-[11px] font-medium text-rust-soft hover:bg-rust/10 disabled:opacity-50 transition-colors">Purge</button>
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
