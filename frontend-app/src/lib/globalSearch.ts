// =============================================================================
// lib/globalSearch.ts
// -----------------------------------------------------------------------------
// WHY THIS EXISTS
// The header search box (see Layout.tsx's submitHeaderSearch()) used to be
// a one-trick "always filter Inventory" box. It now doubles as a single
// jump-to-anything field -- a checkout receipt's "CO-<id>" barcode value
// (see lib/receipt.ts's buildReceiptCode()), a Quotation's "QT-000003"
// reference number (see backend's _reference_number()), or a plain asset/
// tag name -- typed or scanned, then Enter.
//
// This module is pure classification: given the raw typed string, decide
// which of the three destinations it's aimed at. It does no navigation, no
// API calls, and no React -- that all stays in Layout.tsx, which is the
// only place with the auth/role context (privileged vs self-service) and
// the router/drawer handles needed to actually act on the result.
// =============================================================================

export type GlobalSearchTarget =
  | { kind: "checkout"; checkoutId: number }
  | { kind: "quotation"; referenceNumber: string }
  | { kind: "asset"; query: string };

// Matches a checkout receipt code -- "CO-12", "CO12", "co-12" -- same
// digits-only extraction CustodyDrawer's own handleScanSubmit() already
// does for the manual scan-to-find box, just anchored to the whole input
// so a plain asset search like "Cooling fan" never gets misread as one.
const CHECKOUT_CODE_RE = /^co-?(\d+)$/i;

// Matches a Quotation reference number -- "QT-000003", "QT3", "qt-3" --
// the leading zeros _reference_number() pads with are optional here since
// this is what a person actually types/reads off a printed quote, not
// what the backend stores.
const QUOTATION_REF_RE = /^qt-?0*(\d+)$/i;

export function classifyGlobalSearch(raw: string): GlobalSearchTarget {
  const q = raw.trim();

  const checkoutMatch = q.match(CHECKOUT_CODE_RE);
  if (checkoutMatch) return { kind: "checkout", checkoutId: Number(checkoutMatch[1]) };

  const quotationMatch = q.match(QUOTATION_REF_RE);
  if (quotationMatch) return { kind: "quotation", referenceNumber: `QT-${quotationMatch[1].padStart(6, "0")}` };

  return { kind: "asset", query: q };
}
