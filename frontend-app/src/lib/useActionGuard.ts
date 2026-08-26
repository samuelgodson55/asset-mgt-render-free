import { useCallback, useRef } from "react";

/**
 * Returns `guard(fn)`: wraps an async action so a second, near-simultaneous
 * invocation (a double-click, or a touch device's duplicate tap) is a
 * silent no-op instead of firing the action a second time.
 *
 * WHY THIS EXISTS, AND WHY IT'S A REF NOT THE EXISTING `submitting` STATE
 * ------------------------------------------------------------------
 * Every submit button in this app already does `disabled={submitting}`
 * with `setSubmitting(true)` as the first line of the handler. That's
 * correct for the NORMAL case (it keeps the button visually disabled and
 * unclickable for the whole request), but React state updates are
 * batched and only take effect on the next render -- which is not
 * guaranteed to happen before the browser dispatches a second `click`
 * event from a fast double-click or double-tap. In that narrow window,
 * `onClick` can fire twice with the button's `disabled` attribute still
 * `false` in the actual DOM, running the whole async handler (and
 * therefore sending a whole second `fetch()`) twice.
 *
 * Checking a plain ref FIRST, synchronously, before any `await` or
 * `setState`, closes that gap completely: the second invocation returns
 * immediately, before it ever reaches the network -- which is strictly
 * cheaper than letting the duplicate request go out and rely on the
 * backend to dedupe it.
 *
 * This is the first, cheapest line of defense against exactly the
 * scenario backend/services/idempotency.py's `run_idempotent()` (see
 * lib/idempotency.ts's `newIdempotencyKey()` for the client side of
 * that) exists to handle -- the two are complementary, not redundant:
 * this guard stops a duplicate click from ever being SENT; the
 * Idempotency-Key protects the cases this guard can't reach (two
 * separate browser tabs submitting the same cart, a deliberate "Try
 * again" racing a first attempt that hadn't actually finished yet).
 */
export function useActionGuard() {
  const inFlight = useRef(false);

  return useCallback(async <T,>(fn: () => Promise<T>): Promise<T | undefined> => {
    if (inFlight.current) return undefined;
    inFlight.current = true;
    try {
      return await fn();
    } finally {
      inFlight.current = false;
    }
  }, []);
}
