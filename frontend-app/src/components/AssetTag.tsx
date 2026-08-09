import { motion } from "framer-motion";
import type { AssetType } from "../lib/types";
import { formatPrice } from "../lib/api";
import { StatusPill } from "./StatusPill";

interface Props {
  asset: AssetType;
  index?: number;
  onSelect?: (a: AssetType) => void;
  /** From useAuth()'s canSeeStock -- Manager/Admin/Super Admin/demo always
   * true; a Staff/Customer session only when
   * CATALOG_SHOW_STOCK_TO_STAFF_CUSTOMER is on. Hides the
   * available/total quantity readout, its progress bar, and the in-stock/
   * low/out StatusPill, mirroring what the Quotation Catalog already
   * withholds from the same roles (see lib/roles.ts's canSeeStock()). */
  showStock: boolean;
}

export function AssetTag({ asset, index = 0, onSelect, showStock }: Props) {
  const pct = asset.total_quantity ? Math.round((asset.available_quantity / asset.total_quantity) * 100) : 0;

  return (
    <motion.button
      onClick={() => onSelect?.(asset)}
      initial={{ opacity: 0, y: 14 }}
      whileInView={{ opacity: 1, y: 0 }}
      viewport={{ once: true, margin: "-40px" }}
      transition={{ duration: 0.35, delay: Math.min(index * 0.04, 0.4), ease: [0.16, 1, 0.3, 1] }}
      whileHover={{ y: -3 }}
      className="group relative w-full text-left"
    >
      <div className="tag-notch relative flex items-stretch bg-surface border border-border hover:border-brass/60 transition-colors duration-200 rounded-[2px] overflow-hidden pr-6">
        {/* punch hole */}
        <div className="absolute left-3 top-1/2 -translate-y-1/2 w-2.5 h-2.5 rounded-full bg-ink border border-border-soft z-10" />

        <div className="flex-1 pl-8 pr-4 py-4">
          <div className="flex items-start justify-between gap-3">
            <div>
              <p className="font-mono text-[11px] tracking-widest text-brass-soft/90">{asset.tag}</p>
              <h3 className="font-display text-[15px] font-medium text-text mt-1 leading-snug group-hover:text-brass-soft transition-colors">
                {asset.name}
              </h3>
              <p className="text-xs text-text-muted mt-0.5">{asset.category ?? "Uncategorized"}</p>
            </div>
            {showStock && <StatusPill status={asset.status} />}
          </div>

          <div className="mt-4 flex items-end justify-between">
            {showStock ? (
              <div>
                <p className="text-[10px] uppercase tracking-wider text-text-faint">Available</p>
                <p className="font-mono text-lg text-text leading-none mt-1">
                  {asset.available_quantity}
                  <span className="text-text-faint text-sm">/{asset.total_quantity}</span>
                </p>
              </div>
            ) : (
              <span />
            )}
            {asset.price != null && (
              <p className="font-mono text-xs text-text-muted">{formatPrice(asset.price)}</p>
            )}
          </div>

          {showStock && (
            <div className="mt-3 h-[3px] w-full bg-border-soft rounded-full overflow-hidden">
              <motion.div
                initial={{ width: 0 }}
                whileInView={{ width: `${pct}%` }}
                viewport={{ once: true }}
                transition={{ duration: 0.6, delay: 0.1, ease: "easeOut" }}
                className={
                  "h-full rounded-full " +
                  (asset.status === "out" ? "bg-rust" : asset.status === "low" ? "bg-brass" : "bg-moss")
                }
              />
            </div>
          )}
        </div>

        <div className="perforation w-0 border-l border-dashed border-border-soft" />
      </div>
    </motion.button>
  );
}
