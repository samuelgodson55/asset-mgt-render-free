// Constants shared by every table that does TRUE server-side search +
// pagination (Asset Inventory, User Directory, Ad-Hoc Directory, Restore
// Deleted Users/Assets, Audit Trail, Quotations). Kept in their own module
// (rather than alongside components/PaginationBar.tsx) so that file can
// stay component-only -- mixing a non-component export into a component
// file defeats React Fast Refresh for it.

export const PAGE_SIZE_OPTIONS = [5, 10, 25, 50];

// Matches the legacy directories' default (js/ui.js's `tableState` /
// assetsState/usersState/etc. all start at `perPage: 5`).
export const DEFAULT_PAGE_SIZE = 5;

// Quotations-only: on mobile, each catalog row renders as a much taller
// stacked card (name/price, then a Qty/Start/Due grid, then the Add
// button) instead of a table row, so 5 rows per page means a lot more
// scrolling than on desktop. Default to a smaller page there, and add 3
// as a selectable size alongside the shared options so the "Rows per
// page" <select> has a matching option to display.
export const MOBILE_DEFAULT_PAGE_SIZE = 3;
export const MOBILE_PAGE_SIZE_OPTIONS = [3, ...PAGE_SIZE_OPTIONS];
