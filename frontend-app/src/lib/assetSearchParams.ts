// Helpers shared by the Inventory page when synchronizing its URL filters.
// Keeping the URL -> state interpretation in one place makes same-route
// global-search updates deterministic and easy to regression-test.

export type AssetSearchStatus = "all" | "available" | "low" | "out";

export function readAssetSearchParams(params: URLSearchParams): {
  search: string;
  category: string;
  status: AssetSearchStatus;
} {
  const search = params.get("search") ?? "";
  const category = params.get("category") ?? "All";
  const rawStatus = params.get("status");
  const status: AssetSearchStatus =
    rawStatus === "available" || rawStatus === "low" || rawStatus === "out"
      ? rawStatus
      : "all";

  return { search, category, status };
}
