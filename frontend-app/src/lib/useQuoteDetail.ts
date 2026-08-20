// Convenience hook for consuming the quote-detail-drawer context set up by
// `<QuoteDetailProvider>` (see ./quoteDetailContext.tsx). Kept in its own
// file, same reasoning as useAuth.ts/useCustody.ts: keeps the provider
// component and this hook export separate for React Fast Refresh.
import { useContext } from "react";
import { QuoteDetailContext, type QuoteDetailContextValue } from "./quote-detail-context";

export function useQuoteDetail(): QuoteDetailContextValue {
  const ctx = useContext(QuoteDetailContext);
  if (!ctx) throw new Error("useQuoteDetail() must be called within a <QuoteDetailProvider>.");
  return ctx;
}
