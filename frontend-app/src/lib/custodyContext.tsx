import { createContext, useContext, useMemo, useState, type ReactNode } from "react";

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
// =============================================================================

export type CustodyTarget = { type: "user" | "outsider"; id: number; name: string } | null;

interface CustodyContextValue {
  target: CustodyTarget;
  openCustody: (type: "user" | "outsider", id: number, name: string) => void;
  closeCustody: () => void;
}

const CustodyContext = createContext<CustodyContextValue | null>(null);

export function CustodyProvider({ children }: { children: ReactNode }) {
  const [target, setTarget] = useState<CustodyTarget>(null);
  const value = useMemo<CustodyContextValue>(
    () => ({
      target,
      openCustody: (type, id, name) => setTarget({ type, id, name }),
      closeCustody: () => setTarget(null),
    }),
    [target]
  );
  return <CustodyContext.Provider value={value}>{children}</CustodyContext.Provider>;
}

export function useCustody(): CustodyContextValue {
  const ctx = useContext(CustodyContext);
  if (!ctx) throw new Error("useCustody() must be called within a <CustodyProvider>.");
  return ctx;
}
