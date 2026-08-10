import { useContext } from "react";
import { QuoteDetailContext, type QuoteDetailContextValue } from "./quote-detail-context";

export function useQuoteDetail(): QuoteDetailContextValue {
  const ctx = useContext(QuoteDetailContext);
  if (!ctx) throw new Error("useQuoteDetail() must be called within a <QuoteDetailProvider>.");
  return ctx;
}
