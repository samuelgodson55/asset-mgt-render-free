/**
 * lib/clientId.ts
 * -----------------
 * One tiny helper: mint a client-side random identifier. Doesn't need to be
 * cryptographically strong -- just unique enough that two unrelated IDs
 * generated in the same browser session never collide.
 *
 * Shared by:
 *   - lib/idempotency.ts   (Idempotency-Key, minted once per user action)
 *   - lib/api/core.ts      (X-Request-ID, minted once per logical request
 *                            attempt -- see that file for why)
 * so the "UUID with a graceful old-browser fallback" logic lives in exactly
 * one place instead of being copy-pasted per use case.
 */
export function newClientId(prefix: string): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  // Fallback for any environment without crypto.randomUUID (very old
  // browsers, some test runners). The prefix keeps the two use cases
  // visually distinguishable in logs/network tabs even on this fallback
  // path, where a real UUID's shape doesn't do that for us.
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}
