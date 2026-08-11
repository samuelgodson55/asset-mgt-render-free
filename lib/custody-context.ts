import { createContext } from "react";

// Split out of custodyContext.tsx (which now only exports the
// <CustodyProvider> component) so that file satisfies React Fast
// Refresh's "one file, only component exports" rule -- same reasoning,
// and the same three-file shape (X-context.ts / X.tsx / useX.ts), as
// lib/auth-context.ts + lib/auth.tsx + lib/useAuth.ts and
// lib/theme-context.ts + lib/theme.tsx + lib/useTheme.ts already use.
// Pure type/context split; no behavior changed.

// `highlightCheckoutId` is optional and additive -- set only by the global
// header search (see Layout.tsx's submitHeaderSearch()) when it resolves a
// scanned/typed "CO-<id>" checkout code to a holder. CustodyDrawer reads it
// once its items have loaded and reuses its own existing scan-to-find
// mechanism (see handleScanSubmit()) to scroll to and briefly highlight
// that exact row -- every other caller below just omits it and gets the
// drawer's normal "open on this person" behavior, unchanged.
export type CustodyTarget = { type: "user" | "outsider"; id: number; name: string; highlightCheckoutId?: number } | null;

export interface CustodyContextValue {
  target: CustodyTarget;
  openCustody: (type: "user" | "outsider", id: number, name: string, highlightCheckoutId?: number) => void;
  closeCustody: () => void;
}

export const CustodyContext = createContext<CustodyContextValue | null>(null);
