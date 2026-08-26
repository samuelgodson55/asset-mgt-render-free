/**
 * lib/errors.ts
 * -----------------
 * Turns a caught request failure into the three things a person actually
 * needs to see -- a plain-language title, a short explanation, and (when
 * one exists) a correlation ID they can hand to support -- instead of
 * whatever `err.message` happens to contain.
 *
 * BEFORE THIS FILE: every page/drawer had its own local `errMsg(err,
 * fallback)` helper (see pages/Quotations.tsx, pages/MyItems.tsx,
 * components/AssetDrawer.tsx, etc.) that just unwrapped `ApiError.message`
 * or fell back to a generic string, then rendered THAT directly in an
 * <ErrorBanner>. That's how a transient DB pool timeout, an expired
 * session, and a genuine validation mistake all ended up looking
 * identical: one flat rust-colored sentence, no indication of whether
 * anything was actually changed, and the request's X-Request-ID --
 * already captured for ErrorBeacon telemetry, see errorbeacon.ts --
 * never actually shown to the person who'd need to reference it.
 *
 * friendlyError() below is the single place that decides tone/wording by
 * HTTP status; <ErrorState> (components/ui/ErrorState.tsx) is the single
 * place that renders it. Callers just do:
 *
 *   } catch (err) {
 *     setError(friendlyError(err, "Couldn't submit your order."));
 *   }
 *   ...
 *   {error && <ErrorState error={error} onRetry={submitOrder} />}
 */
import { ApiError, MaintenanceModeError } from "./api/core";

export interface FriendlyError {
  /** Short, human summary of what failed. Never a status code or raw exception text. */
  title: string;
  /** One or two plain-language sentences. Safe to show as-is. */
  message: string;
  /** Correlation ID for support, when the failed request returned one. */
  requestId: string | null;
  /** Short positive-toned reassurance line, e.g. "Nothing was changed." Omitted when it can't be said truthfully (e.g. a partial-failure bulk action). */
  reassurance?: string;
  /** Whether a plain retry is likely to help (network hiccups, timeouts, 503s) as opposed to needing the person to change something first (validation, permissions). Callers decide whether to actually offer a Retry button; this is just a hint. */
  retryable: boolean;
}

/**
 * `fallbackTitle` should describe the ACTION that failed in plain language
 * ("Couldn't submit your order.", "Couldn't complete that checkout."), the
 * same string most call sites already pass into the old `errMsg()`
 * helpers -- so converting a call site over is usually a one-line change.
 */
export function friendlyError(err: unknown, fallbackTitle: string, opts: { reassurance?: string } = {}): FriendlyError {
  const reassurance = opts.reassurance ?? "Nothing was changed.";

  if (err instanceof MaintenanceModeError) {
    return {
      title: "The app is undergoing maintenance.",
      message: err.message || "We're performing scheduled maintenance. Please check back shortly.",
      // err.requestId is never a stale/unrelated ID -- see the ApiError
      // branch's comment below for the full explanation of why. In short:
      // core.ts sets it to (in order) a request_id in the JSON body, the
      // X-Request-ID response header, or -- as of the client-minted
      // request ID change -- the ID this exact request attempted to send.
      // That last case covers e.g. nginx's static 503 page during a
      // deploy, before the app is even up to echo one back: the ID is
      // still correct and worth showing, it just means "here's what we
      // tried to send" rather than "here's what the app logged".
      requestId: err.requestId,
      retryable: true,
    };
  }

  if (err instanceof ApiError) {
    // getLastRequestId() (see errorbeacon.ts) is deliberately NEVER used
    // as a fallback here, and never should be: it's the ID of the most
    // recent request that got far enough to receive an X-Request-ID
    // header, which has no guaranteed relationship to THIS failure. The
    // case that surfaced this originally: stop the backend container,
    // click an action -- nginx has nothing to proxy to and returns a bare
    // 502 with no X-Request-ID header and an HTML (non-JSON) body. Back
    // then that left `err.requestId` genuinely null, and a
    // getLastRequestId() fallback would have displayed the ID of some
    // earlier, unrelated, SUCCESSFUL request as this failure's
    // "Reference" -- so a person handing that ID to support (or running
    // scripts/trace-request.sh with it) gets sent to the wrong request
    // entirely, with no indication anything's amiss.
    //
    // The bare-502 case is now handled correctly further upstream instead:
    // core.ts mints a request ID client-side before every request goes
    // out (the same pattern as Idempotency-Key -- see lib/idempotency.ts)
    // and falls back to THAT specific, attempt-scoped ID when neither the
    // response body nor the response header supplied one. So `err.requestId`
    // being null here now only happens for the small remaining set of
    // cases in core.ts where it deliberately isn't set at all (e.g. a
    // fetch()-level failure, handled entirely by the branch at the bottom
    // of this function) -- not for a bare 502, which used to be null but
    // no longer is. ErrorState.tsx renders the "Reference:" chip
    // conditionally (`{error.requestId && (...)}`) either way, so a null
    // value here still just omits it instead of rendering a blank chip.
    const requestId = err.requestId;

    if (err.status === 401) {
      return {
        title: "Your session has expired.",
        message: "Please sign in again to continue.",
        requestId,
        retryable: false,
      };
    }
    if (err.status === 403) {
      return {
        title: "You don't have permission to do that.",
        message: "If you think this is a mistake, ask an admin to check your account's role.",
        requestId,
        retryable: false,
      };
    }
    if (err.status === 404) {
      return {
        title: "We couldn't find that.",
        message: "It may have been moved, returned, or deleted since this page loaded.",
        requestId,
        reassurance,
        retryable: false,
      };
    }
    if (err.status === 409) {
      return {
        title: fallbackTitle,
        // 409s in this app already carry a specific, actionable `detail`
        // (e.g. "This asset was already assigned to someone else") --
        // trust it rather than replacing it with a generic sentence.
        message: err.message || "This may have changed since you loaded the page.",
        requestId,
        reassurance,
        retryable: true,
      };
    }
    if (err.status === 429) {
      return {
        title: "You're doing that a bit too fast.",
        message: "Please wait a moment and try again.",
        requestId,
        retryable: true,
      };
    }
    if (err.status === 503) {
      return {
        title: "The server is temporarily unavailable.",
        message: err.message || "This is usually brief -- please try again in a few seconds.",
        requestId,
        reassurance,
        retryable: true,
      };
    }
    if (err.status >= 500) {
      return {
        title: fallbackTitle,
        message: "Something went wrong on our end. If this keeps happening, contact support with the reference below.",
        requestId,
        reassurance,
        retryable: true,
      };
    }
    // 400/422/other 4xx: this app's own validation messages are already
    // specific and actionable (e.g. "Requested 5 units, but only 2 are
    // available.", "Due date cannot be before the start date.") --
    // showing them as the message (under a plain-language title) is more
    // useful than replacing them with something generic.
    return {
      title: fallbackTitle,
      message: err.message || "Please check what you entered and try again.",
      requestId,
      retryable: false,
    };
  }

  // Not an ApiError at all: fetch() itself threw (offline, DNS failure,
  // CORS, connection dropped mid-request) -- there's no status code and no
  // response body to read a request_id from, and (same reasoning as the
  // ApiError branch above) getLastRequestId() would only ever surface a
  // PREVIOUS, unrelated request's ID here, not one for this failure --
  // never show it as a "Reference" for this error.
  return {
    title: "We couldn't reach the server.",
    message: "Check your internet connection and try again.",
    requestId: null,
    reassurance,
    retryable: true,
  };
}

/**
 * Collapses a FriendlyError into a single line, for the handful of compact
 * single-line status messages in this app (e.g. Profile.tsx's account/
 * password forms) that don't have room for the full <ErrorState> card.
 * Still includes the reference ID when there is one -- that's the one
 * detail worth keeping even in the compact form, since it's the whole
 * point of showing it at all (handing it to support).
 */
export function formatFriendlyErrorLine(f: FriendlyError): string {
  const base = f.message || f.title;
  return f.requestId ? `${base} (Reference: ${f.requestId})` : base;
}
