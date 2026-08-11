import { createContext } from "react";

// Split out of quoteDetailContext.tsx (which now only exports the
// <QuoteDetailProvider> component) so that file satisfies React Fast
// Refresh's "one file, only component exports" rule -- same reasoning,
// and the same three-file shape (X-context.ts / X.tsx / useX.ts), as
// lib/custody-context.ts + lib/custodyContext.tsx + lib/useCustody.ts
// already uses. Pure type/context split; no behavior changed.

export type QuoteDetailMode = "self" | "admin";

export interface QuoteDetailContextValue {
  quotationId: number | null;
  mode: QuoteDetailMode;
  openQuoteDetail: (id: number, mode?: QuoteDetailMode) => void;
  closeQuoteDetail: () => void;
}

export const QuoteDetailContext = createContext<QuoteDetailContextValue | null>(null);
