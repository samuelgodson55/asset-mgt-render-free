import { useEffect, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { Search, SlidersHorizontal, Plus, Download } from "lucide-react";
import { assetsApi } from "../lib/api";
import type { AssetType } from "../lib/types";
import { useAuth } from "../lib/useAuth";
import { isFullAdmin, isPrivileged } from "../lib/roles";
import { AssetTag } from "../components/AssetTag";
import { AssetDrawer } from "../components/AssetDrawer";
import { DispatchModal } from "../components/DispatchModal";
import { CreatePoolModal } from "../components/CreatePoolModal";
import { AssetExportModal } from "../components/AssetExportModal";
import { PaginationBar, RowsPerPageSelect } from "../components/PaginationBar";
import { DEFAULT_PAGE_SIZE } from "../lib/pagination";

export function Assets() {
  const { user, demo, canSeeStock } = useAuth();
  // Every asset-management route (create/edit/delete/restore/checkin/
  // exception/import -- see api/assets_api.py) is require_super_admin,
  // which treats Super Admin and a plain Admin account identically (see
  // deps.py's _FULL_ADMIN_ROLES). Mirrored here as isFullAdmin purely for
  // UI affordance; the backend re-checks every request regardless of what
  // this shows client-side.
  const canManage = demo || isFullAdmin(user?.role);
  const canDispatch = demo || isPrivileged(user?.role);

  const [assets, setAssets] = useState<AssetType[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [perPage, setPerPage] = useState(DEFAULT_PAGE_SIZE);
  const [search, setSearch] = useState("");
  const [category, setCategory] = useState<string>("All");
  const [categories, setCategories] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);

  const [selected, setSelected] = useState<{ id: number; name: string } | null>(null);
  const [dispatching, setDispatching] = useState<{ id: number; name: string; available_quantity: number } | null>(null);
  const [creating, setCreating] = useState(false);
  const [exporting, setExporting] = useState(false);

  const refresh = () => {
    setLoading(true);
    assetsApi.list(perPage, offset, search).then((res) => {
      setAssets(res.items);
      setTotal(res.total);
      setLoading(false);
    });
  };

  useEffect(refresh, [offset, perPage, search]);

  // Called from the "Rows per page" <select> -- always jumps back to the
  // first page on a page-size change (mirrors js/ui.js's setPerPage()).
  const handlePerPageChange = (n: number) => {
    setPerPage(n);
    setOffset(0);
  };

  useEffect(() => {
    assetsApi
      .categories()
      .then((res) => setCategories(res.categories ?? []))
      .catch(() => setCategories([]));
  }, []);

  // Category is a client-side narrowing of the current server-fetched page
  // (there's no `category=` query param on GET /assets) -- same "All" pill
  // behavior as before, just applied on top of the search-narrowed,
  // paginated slice rather than a full client-downloaded snapshot.
  const filtered = category === "All" ? assets : assets.filter((a) => (a.category ?? "Uncategorized") === category);

  return (
    <div>
      <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }} className="flex items-end justify-between mb-5 flex-wrap gap-3">
        <div>
          <h1 className="font-display text-2xl font-semibold text-text">Inventory</h1>
          <p className="text-text-muted text-sm mt-1">{total} asset pool(s)</p>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <div className="relative w-64 max-w-full">
            <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-text-faint" />
            <input
              value={search}
              onChange={(e) => { setOffset(0); setSearch(e.target.value); }}
              placeholder="Search by name…"
              className="w-full bg-surface border border-border-soft rounded-[3px] pl-8 pr-3 py-2 text-[12.5px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none transition-colors"
            />
          </div>
          <RowsPerPageSelect value={perPage} onChange={handlePerPageChange} />
          <button onClick={() => setExporting(true)} className="flex items-center gap-1.5 border border-border-soft hover:border-brass/40 text-[12px] text-text rounded-[3px] px-3 py-2 transition-colors">
            <Download size={12} /> Export
          </button>
          {canManage && (
            <button onClick={() => setCreating(true)} className="flex items-center gap-1.5 bg-brass hover:bg-brass-soft text-ink font-medium text-[12px] rounded-[3px] px-3 py-2 transition-colors">
              <Plus size={12} /> New pool
            </button>
          )}
        </div>
      </motion.div>

      <div className="flex items-center gap-2 mb-5 flex-wrap">
        <SlidersHorizontal size={13} className="text-text-faint" />
        {["All", ...categories].map((c) => (
          <button
            key={c}
            onClick={() => setCategory(c)}
            className={`px-2.5 py-1 rounded-full text-[11.5px] border transition-colors ${
              category === c
                ? "bg-brass/15 border-brass/40 text-brass-soft"
                : "border-border-soft text-text-muted hover:text-text hover:border-border"
            }`}
          >
            {c}
          </button>
        ))}
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-3">
        <AnimatePresence mode="popLayout">
          {filtered.map((a, i) => (
            <AssetTag key={a.id} asset={a} index={i} showStock={canSeeStock} onSelect={(asset) => setSelected({ id: asset.id, name: asset.name })} />
          ))}
        </AnimatePresence>
      </div>

      {!loading && filtered.length === 0 && (
        <div className="text-center py-20 text-text-faint text-sm">No assets match that filter. Try a different tag or category.</div>
      )}

      {category === "All" && (
        <div className="mt-5">
          <PaginationBar total={total} perPage={perPage} offset={offset} onOffsetChange={setOffset} />
        </div>
      )}

      <AssetDrawer
        asset={selected}
        onClose={() => setSelected(null)}
        canManage={canManage}
        canDispatch={canDispatch}
        showStock={canSeeStock}
        onDispatch={(a) => setDispatching(a)}
        onChanged={refresh}
      />

      <DispatchModal
        asset={dispatching}
        onClose={() => setDispatching(null)}
        onDispatched={() => {
          setDispatching(null);
          setSelected(null);
          refresh();
        }}
      />

      <CreatePoolModal open={creating} onClose={() => setCreating(false)} onCreated={() => { setCreating(false); refresh(); }} />
      <AssetExportModal open={exporting} onClose={() => setExporting(false)} />
    </div>
  );
}
