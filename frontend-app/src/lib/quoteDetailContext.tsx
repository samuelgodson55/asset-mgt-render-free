import { useMemo, useState, type ReactNode } from "react";
import { QuoteDetailContext, type QuoteDetailContextValue, type QuoteDetailMode } from "./quote-detail-context";

// =============================================================================
// lib/quoteDetailContext.tsx
// -----------------------------------------------------------------------------
// WHY THIS EXISTS
// The Notification Bell's "Quotation updates" click-through used to
// navigate to /quotations?quotation=<id> and rely on Quotations.tsx's own
// one-shot (`[]`-dependency) useEffect to notice the `?quotation=` param
// and open its LOCAL <QuoteDetailDrawer/> -- fine the very first time
// Notifications->Quotations is visited, but if the Quotations page was
// already mounted (or the person clicked a second notification without
// the drawer having ever unmounted the page in between), that effect
// simply never re-ran, so the click quietly landed on the general
// Quotations page instead of the exact quote.
//
// This mirrors the exact fix already applied to the Custody Ledger drawer
// (see lib/custodyContext.tsx's own docstring for the full history): one
// shared "My Quote Detail" drawer, owned here above the router's page
// content (see Layout.tsx), that ANY page -- a notification row chief
// among them -- can open directly with a plain function call. No
// navigation, no query-string parsing, no page/mount-order race. Same
// "View ->" shape Notifications.tsx already uses for Overdue/Due Soon/
// Extension Requests via useCustody().
//
// The context/types themselves live in lib/quote-detail-context.ts and
// the hook in lib/useQuoteDetail.ts -- this file exports ONLY the
// <QuoteDetailProvider> component so React Fast Refresh can reliably
// hot-reload it, same split lib/custody-context.ts/custodyContext.tsx/
// useCustody.ts already uses.
// =============================================================================

export function QuoteDetailProvider({ children }: { children: ReactNode }) {
  const [quotationId, setQuotationId] = useState<number | null>(null);
  const [mode, setMode] = useState<QuoteDetailMode>("self");
  const value = useMemo<QuoteDetailContextValue>(
    () => ({
      quotationId,
      mode,
      openQuoteDetail: (id, nextMode = "self") => {
        setMode(nextMode);
        setQuotationId(id);
      },
      closeQuoteDetail: () => {
        setQuotationId(null);
        setMode("self");
      },
    }),
    [quotationId, mode]
  );
  return <QuoteDetailContext.Provider value={value}>{children}</QuoteDetailContext.Provider>;
}
