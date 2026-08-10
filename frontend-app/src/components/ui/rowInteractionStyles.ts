// =============================================================================
// components/ui/rowInteractionStyles.ts
// -----------------------------------------------------------------------------
// Plain helper (not a component) for RowDetails.tsx's mobile-tappable-row
// pattern -- split into its own .ts file, same reasoning as formStyles.ts,
// so RowDetails.tsx stays component-only for React Fast Refresh.
// =============================================================================

// Same "cursor-pointer ... sm:cursor-default" treatment every legacy table
// row used -- tappable below `sm` where the rest of the columns/actions are
// hidden, inert (desktop keeps using the inline buttons) at `sm` and up.
export function mobileRowClass(extra = "") {
  return `cursor-pointer transition-colors hover:bg-surface-raised active:bg-surface-raised sm:cursor-default sm:hover:bg-transparent sm:active:bg-transparent ${extra}`.trim();
}
