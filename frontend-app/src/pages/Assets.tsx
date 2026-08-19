import { useCallback, useEffect, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { Search, SlidersHorizontal, Plus, Download } from "lucide-react";
import { useSearchParams } from "react-router-dom";
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
import { readAssetSearchParams } from "../lib/assetSearchParams";
import { useRequestGuard } from "../lib/useRequestGuard";

const STATUS_TABS: { key: "all" | AssetType["status"]; label: string }[] = [
  { key: "all", label: "Any status" },
  { key: "available", label: "In stock" },
  { key: "low", label: "Low" },
  { key: "out", label: "Out" },
];

export function Assets() {
  const { user, demo, canSeeStock } = useAuth();
  // Deep-link support (?category=&status=&search=) -- lets a StatCard on
  // the Dashboard, the header search bar, or a bookmarked/shared link land
  // straight on the filtered view it promised instead of dumping the
  // person on the unfiltered inventory and making them redo the filtering
  // by hand. Read once on mount; every filter change below keeps the URL
  // in sync afterward so the address bar always reflects what's on screen
  // (shareable + survives a refresh), the same "efficient site" pattern
  // GitHub/Linear-style filtered list views use.
  const [searchParams, setSearchParams] = useSearchParams();
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
  const [search, setSearch] = useState(() => searchParams.get("search") ?? "");
  const [category, setCategory] = useState<string>(() => searchParams.get("category") ?? "All");
  const [status, setStatus] = useState<(typeof STATUS_TABS)[number]["key"]>(() => readAssetSearchParams(searchParams).status);
  const [categories, setCategories] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);

  // The global header search can navigate to /assets with a new ?search=
  // value while this page is already mounted. In that case React Router
  // updates the URL but does not remount Assets, so the old local search
  // would otherwise remain visible and keep filtering the previous query.
  // Keep the page filters synchronized with URL changes from either source:
  // the local controls or the global header search.
  useEffect(() => {
    const { search: nextSearch, category: nextCategory, status: nextStatus } =
      readAssetSearchParams(searchParams);

    setSearch((current) => (current === nextSearch ? current : nextSearch));
    setCategory((current) => (current === nextCategory ? current : nextCategory));
    setStatus((current) => (current === nextStatus ? current : nextStatus));
    setOffset(0);
  }, [searchParams]);

  const [selected, setSelected] = useState<{ id: number; name: string } | null>(null);
  const [dispatching, setDispatching] = useState<{ id: number; name: string; available_quantity: number } | null>(null);
  const [creating, setCreating] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const beginRequest = useRequestGuard();

  const refresh = useCallback(() => {
    const isCurrent = beginRequest();
    setLoading(true);
    assetsApi.list(perPage, offset, search, category, status === "all" ? undefined : status).then((res) => {
      if (!isCurrent()) return;
      setError(null);
      setAssets(res.items);
      setTotal(res.total);
      setLoading(false);
    }).catch((err) => {
      if (!isCurrent()) return;
      setError(err instanceof Error ? err.message : "Couldn't load the asset inventory.");
      setLoading(false);
    });
  }, [beginRequest, perPage, offset, search, category, status]);

  useEffect(refresh, [refresh]);

  useEffect(() => {
    if (offset > 0 && offset >= total) setOffset(Math.max(0, Math.floor(Math.max(total - 1, 0) / perPage) * perPage));
  }, [offset, total, perPage]);

  // Called from the "Rows per page" <select> -- always jumps back to the
  // first page on a page-size change (mirrors js/ui.js's setPerPage()).
  const handlePerPageChange = (n: number) => {
    setPerPage(n);
    setOffset(0);
  };

  useEffect(() => {
    let cancelled = false;
    assetsApi
      .categories()
      .then((res) => { if (!cancelled) setCategories(res.categories ?? []); })
      .catch(() => { if (!cancelled) setCategories([]); });
    return () => { cancelled = true; };
  }, []);

  // Search, category, and stock status are all server-side filters. The page
  // therefore renders the returned slice directly; no filter is applied
  // after pagination, so totals and later pages remain correct.
  const filtered = assets;

  // Pill/tab clicks (and the search box) update both local state and the
  // URL together, so the address bar always mirrors what's currently on
  // screen -- a StatCard, a bookmark, or hitting "back" all land on the
  // exact same filtered view rather than the unfiltered default.
  const changeCategory = (c: string) => {
    setOffset(0);
    setCategory(c);
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      if (c === "All") next.delete("category");
      else next.set("category", c);
      return next;
    }, { replace: true });
  };

  const changeStatus = (s: (typeof STATUS_TABS)[number]["key"]) => {
    setStatus(s);
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      if (s === "all") next.delete("status");
      else next.set("status", s);
      return next;
    }, { replace: true });
  };

  const changeSearch = (q: string) => {
    setOffset(0);
    setSearch(q);
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      if (!q) next.delete("search");
      else next.set("search", q);
      return next;
    }, { replace: true });
  };

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
              onChange={(e) => changeSearch(e.target.value)}
              placeholder="Search by name…"
              className="w-full bg-surface border border-border-soft rounded-[3px] pl-8 pr-3 py-2 text-[12.5px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none transition-colors"
            />
          </div>
          <RowsPerPageSelect value={perPage} onChange={handlePerPageChange} />
          <button onClick={() => setExporting(true)} className="flex items-center gap-1.5 border border-border-soft hover:border-brass/40 text-[12px] text-text rounded-[3px] px-3 py-2 transition-colors">
            <Download size={12} /> Export
          </button>
          {canManage && (
            <button data-otel-action="asset.create.open" onClick={() => setCreating(true)} className="flex items-center gap-1.5 bg-brass hover:bg-brass-soft text-ink font-medium text-[12px] rounded-[3px] px-3 py-2 transition-colors">
              <Plus size={12} /> New pool
            </button>
          )}
        </div>
      </motion.div>

      {error && (
        <div className="mb-4 rounded-[3px] border border-rust/30 bg-rust/10 px-3 py-2.5 text-[12px] text-rust-soft">
          {error}
        </div>
      )}

      <div className="flex items-center gap-2 mb-3 flex-wrap">
        <SlidersHorizontal size={13} className="text-text-faint" />
        {["All", ...categories].map((c) => (
          <button
            key={c}
            onClick={() => changeCategory(c)}
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

      {canSeeStock && (
        <div className="flex items-center gap-2 mb-5 flex-wrap">
          <span className="w-[13px]" />
          {STATUS_TABS.map((s) => (
            <button
              key={s.key}
              onClick={() => changeStatus(s.key)}
              className={`px-2.5 py-1 rounded-full text-[11.5px] border transition-colors ${
                status === s.key
                  ? "bg-sky/15 border-sky/40 text-sky"
                  : "border-border-soft text-text-muted hover:text-text hover:border-border"
              }`}
            >
              {s.label}
            </button>
          ))}
        </div>
      )}

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

      {/* Category is now server-filtered, so `total`/pagination are always
          correct for whatever category is selected -- no need to hide the
          bar for it anymore. Status is still a client-side narrowing of
          the current page (see `filtered` above), so paging while a
          status filter is active would still be misleading; the bar stays
          hidden for that case only. */}
      {status === "all" && (
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
