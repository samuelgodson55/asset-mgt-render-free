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
