/**
 * lib/api/format.ts
 * ----------------------
 * Mirrors the legacy frontend's js/ui.js formatPrice()/setCurrencyCode()
 * -- defaults to Naira, overridable at runtime once
 * quotationsApi.publicConfig() resolves the real deployment value
 * (settings.CURRENCY_CODE). Also relativeTime()/formatDate() -- small
 * display helpers used across every page's tables.
 */

let _currencyCode = "NGN";

export function setCurrencyCode(code: string | null | undefined) {
  if (code) _currencyCode = code;
}

export function formatPrice(price: number | null | undefined): string {
  if (price === null || price === undefined) return "—";
  try {
    return new Intl.NumberFormat("en-NG", { style: "currency", currency: _currencyCode }).format(price);
  } catch {
    // Unknown/unsupported currency code -- fall back to a plain number
    // rather than letting Intl throw and break the whole render.
    return `${_currencyCode} ${Number(price).toFixed(2)}`;
  }
}

export function relativeTime(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const abs = Math.abs(diff);
  const mins = Math.round(abs / 60000);
  const hours = Math.round(abs / 3600000);
  const days = Math.round(abs / 86400000);
  const suffix = diff >= 0 ? "ago" : "from now";
  if (mins < 60) return `${mins}m ${suffix}`;
  if (hours < 24) return `${hours}h ${suffix}`;
  return `${days}d ${suffix}`;
}

export function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}
