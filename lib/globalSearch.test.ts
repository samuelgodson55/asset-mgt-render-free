import { describe, it, expect } from "vitest";
import { classifyGlobalSearch } from "./globalSearch";

// =============================================================================
// lib/globalSearch.test.ts
// -----------------------------------------------------------------------------
// Covers classifyGlobalSearch()'s three destinations -- checkout code,
// Quotation reference, and the plain-asset fallback -- since Layout.tsx's
// submitHeaderSearch() branches its whole async lookup/navigation flow on
// whichever `kind` this returns. See globalSearch.ts's own docstring for
// why the classification is kept pure and separate from that flow.
// =============================================================================

describe("classifyGlobalSearch", () => {
  it("recognizes a checkout receipt code in every case/dash variant", () => {
    expect(classifyGlobalSearch("CO-12")).toEqual({ kind: "checkout", checkoutId: 12 });
    expect(classifyGlobalSearch("co12")).toEqual({ kind: "checkout", checkoutId: 12 });
    expect(classifyGlobalSearch("Co-0007")).toEqual({ kind: "checkout", checkoutId: 7 });
    expect(classifyGlobalSearch("  co-3  ")).toEqual({ kind: "checkout", checkoutId: 3 });
  });

  it("recognizes a Quotation reference number, padding it to the backend's 6-digit shape", () => {
    expect(classifyGlobalSearch("QT-000003")).toEqual({ kind: "quotation", referenceNumber: "QT-000003" });
    expect(classifyGlobalSearch("qt3")).toEqual({ kind: "quotation", referenceNumber: "QT-000003" });
    expect(classifyGlobalSearch("QT-3")).toEqual({ kind: "quotation", referenceNumber: "QT-000003" });
  });

  it("falls back to a plain asset search for anything else", () => {
    expect(classifyGlobalSearch("Sony FX3")).toEqual({ kind: "asset", query: "Sony FX3" });
    expect(classifyGlobalSearch("fx6 card 960Gb")).toEqual({ kind: "asset", query: "fx6 card 960Gb" });
    // A bare "CO" or "QT" with no digits attached isn't a code -- it's
    // just as likely to be the start of an asset's own name (a "Cooling
    // fan" or a "QT-branded" something), so it should still fall through.
    expect(classifyGlobalSearch("CO")).toEqual({ kind: "asset", query: "CO" });
    expect(classifyGlobalSearch("QT")).toEqual({ kind: "asset", query: "QT" });
  });

  it("trims surrounding whitespace before classifying", () => {
    expect(classifyGlobalSearch("  Lexar Professional  ")).toEqual({ kind: "asset", query: "Lexar Professional" });
  });
});
