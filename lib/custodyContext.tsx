import { useMemo, useState, type ReactNode } from "react";
import { CustodyContext, type CustodyContextValue, type CustodyTarget } from "./custody-context";

// =============================================================================
// lib/custodyContext.tsx
// -----------------------------------------------------------------------------
// WHY THIS EXISTS
// The Custody Ledger drawer used to be local state owned separately by
// UsersPanel and OutsidersPanel (each rendering its own <CustodyDrawer/>),
// with the Notifications page's "View ->" click-through reaching it by
// navigating to /admin?custody=type:id&name=... and hoping Admin.tsx's tab
// switch + a deep-link useEffect inside whichever panel matched fired
// before anything else re-rendered it away. That chain was fragile in
// practice -- clicking "View ->" would land on the right directory tab
// but the drawer itself would silently fail to open.
//
// Legacy (js/components/custody.js's openCustodyModal()) never had this
// problem because it never routed through a page/tab at all: the Custody
// Ledger is ONE shared modal, opened directly by id+type from wherever the
// click happened (a notification row, a directory row, an extension
// request) -- no navigation, no tab state, no deep-link parsing required.
// This context ports that same shape to the React app: one drawer, owned
// above the router's page content (see Layout.tsx), that any page can open
// with a plain function call.
//
// The context/types themselves live in lib/custody-context.ts and the hook
// in lib/useCustody.ts -- this file exports ONLY the <CustodyProvider>
// component so React Fast Refresh can reliably hot-reload it (Fast Refresh
// requires a component-only file; mixing in a hook/context export here
// used to trip oxlint's react/only-export-components warning and meant an
// edit to this file could silently fall back to a full page reload instead
// of a fast in-place swap). Same three-file split lib/auth-context.ts +
// lib/auth.tsx + lib/useAuth.ts and lib/theme-context.ts + lib/theme.tsx +
// lib/useTheme.ts already use.
// =============================================================================

export function CustodyProvider({ children }: { children: ReactNode }) {
  const [target, setTarget] = useState<CustodyTarget>(null);
  const value = useMemo<CustodyContextValue>(
    () => ({
      target,
      openCustody: (type, id, name, highlightCheckoutId) => setTarget({ type, id, name, highlightCheckoutId }),
      closeCustody: () => setTarget(null),
    }),
    [target]
  );
  return <CustodyContext.Provider value={value}>{children}</CustodyContext.Provider>;
}
