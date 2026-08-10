import { createContext } from "react";

// Split out of custodyContext.tsx (which now only exports the
// <CustodyProvider> component) so that file satisfies React Fast
// Refresh's "one file, only component exports" rule -- same reasoning,
// and the same three-file shape (X-context.ts / X.tsx / useX.ts), as
// lib/auth-context.ts + lib/auth.tsx + lib/useAuth.ts and
// lib/theme-context.ts + lib/theme.tsx + lib/useTheme.ts already use.
// Pure type/context split; no behavior changed.

export type CustodyTarget = { type: "user" | "outsider"; id: number; name: string } | null;

export interface CustodyContextValue {
  target: CustodyTarget;
  openCustody: (type: "user" | "outsider", id: number, name: string) => void;
  closeCustody: () => void;
}

export const CustodyContext = createContext<CustodyContextValue | null>(null);
