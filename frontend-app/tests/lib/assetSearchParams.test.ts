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
    const first = readAssetSearchParams(new URLSearchParams("search=fx3"));
    const second = readAssetSearchParams(new URLSearchParams("search=fx9"));

    expect(first.search).toBe("fx3");
    expect(second.search).toBe("fx9");
  });

  it("falls back safely for missing or invalid filters", () => {
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
