// Guards the double-click fix described in lib/useActionGuard.ts: a second,
// near-simultaneous invocation of a guarded action must be a no-op (never
// re-run the wrapped function) while the first is still in flight, but a
// later, genuinely sequential call (after the first has settled) must run
// normally.
import { renderHook, act } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { useActionGuard } from "../../src/lib/useActionGuard";
import { newIdempotencyKey } from "../../src/lib/idempotency";

function deferred<T>() {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((r) => { resolve = r; });
  return { promise, resolve };
}

describe("useActionGuard", () => {
  it("ignores a second call while the first is still in flight", async () => {
    const { result } = renderHook(() => useActionGuard());
    const calls: number[] = [];
    const gate = deferred<void>();

    let firstPromise!: Promise<string | undefined>;
    let secondPromise!: Promise<string | undefined>;

    act(() => {
      firstPromise = result.current(async () => {
        calls.push(1);
        await gate.promise;
        return "first-result";
      });
      // Fired synchronously right after, simulating a double-click that
      // beats React's re-render of the button's `disabled` attribute.
      secondPromise = result.current(async () => {
        calls.push(2);
        return "second-result";
      });
    });

    await act(async () => {
      gate.resolve();
      await Promise.all([firstPromise, secondPromise]);
    });

    // The guarded function only ran once -- the duplicate invocation
    // returned undefined without ever executing its body.
    expect(calls).toEqual([1]);
    expect(await firstPromise).toBe("first-result");
    expect(await secondPromise).toBeUndefined();
  });

  it("allows a later call once the previous one has settled", async () => {
    const { result } = renderHook(() => useActionGuard());
    const calls: number[] = [];

    await act(async () => {
      await result.current(async () => { calls.push(1); return "a"; });
    });
    await act(async () => {
      await result.current(async () => { calls.push(2); return "b"; });
    });

    expect(calls).toEqual([1, 2]);
  });

  it("releases the guard even if the wrapped action throws, so a retry after a real failure isn't blocked", async () => {
    const { result } = renderHook(() => useActionGuard());
    const calls: number[] = [];

    await act(async () => {
      await expect(result.current(async () => { calls.push(1); throw new Error("boom"); })).rejects.toThrow("boom");
    });
    await act(async () => {
      await result.current(async () => { calls.push(2); return "ok"; });
    });

    expect(calls).toEqual([1, 2]);
  });
});

describe("newIdempotencyKey", () => {
  it("returns a non-empty string, and a fresh value on every call", () => {
    const a = newIdempotencyKey();
    const b = newIdempotencyKey();
    expect(typeof a).toBe("string");
    expect(a.length).toBeGreaterThan(0);
    expect(a).not.toBe(b);
  });
});
