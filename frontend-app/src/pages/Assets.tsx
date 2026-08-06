import { useEffect, useMemo, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { Search, SlidersHorizontal } from "lucide-react";
import { api } from "../lib/api";
import type { AssetType } from "../lib/types";
import { AssetTag } from "../components/AssetTag";
import { AssetDrawer } from "../components/AssetDrawer";

export function Assets() {
  const [assets, setAssets] = useState<AssetType[]>([]);
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState<string>("All");
  const [selected, setSelected] = useState<AssetType | null>(null);

  useEffect(() => {
    api.getAssets().then(setAssets);
  }, []);

  const categories = useMemo(() => ["All", ...Array.from(new Set(assets.map((a) => a.category ?? "Uncategorized")))], [assets]);

  const filtered = assets.filter((a) => {
    const matchesQuery = (a.name + a.tag).toLowerCase().includes(query.toLowerCase());
    const matchesCat = category === "All" || (a.category ?? "Uncategorized") === category;
    return matchesQuery && matchesCat;
  });

  return (
    <div>
      <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }} className="flex items-end justify-between mb-5 flex-wrap gap-3">
        <div>
          <h1 className="font-display text-2xl font-semibold text-text">Inventory</h1>
          <p className="text-text-muted text-sm mt-1">{filtered.length} of {assets.length} asset pools</p>
        </div>
        <div className="relative w-72 max-w-full">
          <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-text-faint" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Filter by name or tag…"
            className="w-full bg-surface border border-border-soft rounded-[3px] pl-8 pr-3 py-2 text-[12.5px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none transition-colors"
          />
        </div>
      </motion.div>

      <div className="flex items-center gap-2 mb-5 flex-wrap">
        <SlidersHorizontal size={13} className="text-text-faint" />
        {categories.map((c) => (
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
            <AssetTag key={a.id} asset={a} index={i} onSelect={setSelected} />
          ))}
        </AnimatePresence>
      </div>

      {filtered.length === 0 && (
        <div className="text-center py-20 text-text-faint text-sm">No assets match that filter. Try a different tag or category.</div>
      )}

      <AssetDrawer asset={selected} onClose={() => setSelected(null)} />
    </div>
  );
}
