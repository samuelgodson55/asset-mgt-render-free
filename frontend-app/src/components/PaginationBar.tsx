// Shared controls for every table that does TRUE server-side search +
// pagination (Asset Inventory, User Directory, Ad-Hoc Directory, Restore
// Deleted Users/Assets, Audit Trail, Quotations) -- mirrors the legacy
// frontend's "Rows per page" <select> (js/ui.js's setPerPage()) and
// "Showing X-Y of Z" + Prev/Next bar (renderServerPaginationBar()) in
// js/ui.js, wired to js/components/*.js's own small `{ page, perPage,
// total }` state objects (assetsState, usersState, etc).
//
// Each page keeps its own `perPage`/`offset` state (just like each legacy
// component keeps its own `*State` object) and passes it down here --
// this file only renders the controls and reports changes back up.
//
// PAGE_SIZE_OPTIONS/DEFAULT_PAGE_SIZE live in lib/pagination.ts, not here --
// this file exports components only, so React Fast Refresh keeps working
// for it (see that module's docstring).

import { PAGE_SIZE_OPTIONS, DEFAULT_PAGE_SIZE } from "../lib/pagination";

export function RowsPerPageSelect({
  value,
  onChange,
  options = PAGE_SIZE_OPTIONS,
}: {
  value: number;
  onChange: (n: number) => void;
  /** Defaults to the shared PAGE_SIZE_OPTIONS; pass a page-specific list
   * (e.g. Quotations' MOBILE_PAGE_SIZE_OPTIONS) when that page offers a
   * size the shared list doesn't. */
  options?: number[];
}) {
  return (
    <div className="flex items-center gap-2 text-[12px] text-text-muted whitespace-nowrap">
      <span>Rows per page</span>
      <select
        value={value}
        onChange={(e) => onChange(parseInt(e.target.value, 10) || DEFAULT_PAGE_SIZE)}
        className="bg-surface border border-border-soft rounded-[3px] px-2 py-1.5 text-text focus:border-brass/50 focus:outline-none"
      >
        {options.map((n) => (
          <option key={n} value={n}>
            {n}
          </option>
        ))}
      </select>
    </div>
  );
}

export function PaginationBar({
  total,
  perPage,
  offset,
  onOffsetChange,
}: {
  total: number;
  perPage: number;
  offset: number;
  onOffsetChange: (n: number) => void;
}) {
  const shownCount = Math.min(perPage, Math.max(0, total - offset));
  return (
    <div className="flex items-center justify-between text-[12px] text-text-muted flex-wrap gap-2">
      <span>{total === 0 ? "No results found." : `${offset + 1}–${offset + shownCount} of ${total}`}</span>
      <div className="flex gap-2">
        <button
          disabled={offset === 0}
          onClick={() => onOffsetChange(Math.max(0, offset - perPage))}
          className="border border-border-soft rounded-[3px] px-2.5 py-1 disabled:opacity-40"
        >
          Prev
        </button>
        <button
          disabled={offset + perPage >= total}
          onClick={() => onOffsetChange(offset + perPage)}
          className="border border-border-soft rounded-[3px] px-2.5 py-1 disabled:opacity-40"
        >
          Next
        </button>
      </div>
    </div>
  );
}
