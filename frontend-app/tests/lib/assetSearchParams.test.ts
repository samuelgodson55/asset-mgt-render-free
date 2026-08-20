// Covers src/lib/assetSearchParams.ts: parsing the Assets page's URL query
// string (?search=&category=&status=) into a typed filter object, with
// safe fallback defaults for anything missing or invalid -- e.g. deep
// links or manually-edited URLs shouldn't be able to put the page filters
// into an unrecognized state.
import { describe, expect, it } from "vitest";
import { readAssetSearchParams } from "../../src/lib/assetSearchParams";

describe("readAssetSearchParams", () => {
  it("reads a new global search query from the current URL", () => {
    expect(readAssetSearchParams(new URLSearchParams("search=fx9"))).toEqual({
      search: "fx9",
      category: "All",
      status: "all",
    });
  });

  it("does not retain a previous query when the URL changes", () => {
    // Guards against stale-closure-style bugs: each call must derive its
    // result purely from the URLSearchParams passed in, not from any
    // module-level state left over from a prior call.
    const first = readAssetSearchParams(new URLSearchParams("search=fx3"));
    const second = readAssetSearchParams(new URLSearchParams("search=fx9"));

    expect(first.search).toBe("fx3");
    expect(second.search).toBe("fx9");
  });

  it("falls back safely for missing or invalid filters", () => {
    // No params at all, and a `status` value outside the known enum,
    // should both resolve to the same safe defaults rather than throwing
    // or passing the invalid value through.
    expect(readAssetSearchParams(new URLSearchParams())).toEqual({
      search: "",
      category: "All",
      status: "all",
    });
    expect(readAssetSearchParams(new URLSearchParams("status=invalid"))).toEqual({
      search: "",
      category: "All",
      status: "all",
    });
  });
});
