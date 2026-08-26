/**
 * lib/idempotency.ts
 * -------------------
 * One function: mint a fresh Idempotency-Key value, once per logical
 * "user action" (a single button click/submit), for the small set of
 * high-stakes mutating calls that opt in (checkout/dispatch, submit
 * cart, create pool -- see backend/services/idempotency.py for why
 * these three).
 *
 * THE KEY MUST BE GENERATED ONCE PER CLICK, NOT ONCE PER HTTP ATTEMPT.
 * The whole point is that if a double-click (or a client-side retry of
 * the exact same submit) fires two physically separate `fetch()` calls
 * for what the person experienced as ONE action, both requests carry the
 * SAME key -- that's what lets the backend recognize them as duplicates
 * and skip re-running the underlying checkout/submit logic a second
 * time, instead of a second attempt racing the first for a row lock and
 * then failing with a "someone already did that" error, or (without an
 * idempotency key at all) actually running twice.
 *
 * Call this at the START of the click handler (e.g. inside
 * `submitOrder()`, before the `try`), NOT inside rawFetch/api.ts's
 * request layer -- rawFetch has no way to know "this call and that
 * other call came from the same button press", only the calling
 * component does.
 */
import { newClientId } from "./clientId";

export function newIdempotencyKey(): string {
  return newClientId("idem");
}
