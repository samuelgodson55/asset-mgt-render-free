// =============================================================================
// components/ui/ErrorBanner.tsx
// -----------------------------------------------------------------------------
// The small inline rust-toned banner used to surface a failed request --
// repeated verbatim across nearly every panel/modal before this was pulled
// out. `icon` is optional since a couple of call sites (the compact form
// errors) never had one.
//
// For a caught API/network failure (as opposed to a simple inline "you
// missed a field" validation message), prefer <ErrorState> below instead --
// it takes a lib/errors.ts FriendlyError (title + plain-language message +
// reference ID + optional retry) rather than a single opaque string, so the
// person sees what actually happened and what to do next instead of a raw
// backend error sentence.
// =============================================================================
import { useState } from "react";
import type { ReactNode } from "react";
import { AlertTriangle, Copy, Check, RotateCcw } from "lucide-react";
import type { FriendlyError } from "../../lib/errors";

export function ErrorBanner({ children, icon }: { children: ReactNode; icon?: ReactNode }) {
  return (
    <div className="flex items-start gap-2 bg-rust/10 border border-rust/30 text-rust-soft text-[12px] rounded-[3px] px-3 py-2.5">
      {icon && <span className="mt-0.5 shrink-0">{icon}</span>}
      <span>{children}</span>
    </div>
  );
}

// -----------------------------------------------------------------------------
// ErrorState -- the "extremely user-friendly" replacement for showing a
// caught request failure. Renders:
//   - a plain-language title ("We couldn't complete that checkout.")
//   - one or two explanatory sentences, never a raw status code or stack
//   - an optional reassurance line ("Nothing was changed.")
//   - a small copyable "Reference: <id>" chip when the failed request
//     returned a correlation ID, so a person can hand it to support instead
//     of having to screenshot a stack trace
//   - action buttons: a primary "Try again" (only when the caller passes
//     `onRetry` -- some failures, like a 403 or an expired session, aren't
//     retryable and this is correctly omitted), any extra `actions`, and a
//     dismiss button when `onDismiss` is passed.
//
// Deliberately still an INLINE element (not a modal) -- this app already
// surfaces every request failure as a dismissible banner near the action
// that failed (see pages/Quotations.tsx's `error` state, AssetDrawer.tsx,
// etc.), and keeping that placement means adopting this component is a
// drop-in swap for the old `{error && <ErrorBanner>{error}</ErrorBanner>}`
// pattern rather than a new interaction model to learn.
export function ErrorState({
  error,
  onRetry,
  onDismiss,
  actions,
}: {
  error: FriendlyError;
  /** Omit for non-retryable failures (permissions, expired session, "not found"). */
  onRetry?: () => void;
  onDismiss?: () => void;
  /** Extra buttons beyond Try again/Dismiss, e.g. a "View asset" link. */
  actions?: Array<{ label: string; onClick: () => void }>;
}) {
  const [copied, setCopied] = useState(false);

  const copyReference = () => {
    if (!error.requestId) return;
    navigator.clipboard?.writeText(error.requestId).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    }).catch(() => {});
  };

  return (
    <div className="bg-rust/10 border border-rust/30 rounded-[3px] px-3.5 py-3 text-[12px]">
      <div className="flex items-start gap-2.5">
        <AlertTriangle size={15} className="mt-0.5 shrink-0 text-rust-soft" />
        <div className="min-w-0 flex-1">
          <p className="font-medium text-rust-soft">{error.title}</p>
          {error.message && <p className="mt-0.5 text-text-muted leading-relaxed">{error.message}</p>}
          {error.reassurance && <p className="mt-1 text-moss-soft">{error.reassurance}</p>}

          {error.requestId && (
            <button
              type="button"
              onClick={copyReference}
              title="Copy reference ID"
              className="mt-2 inline-flex items-center gap-1.5 rounded-[3px] border border-border-soft bg-ink-soft px-2 py-1 font-mono text-[10.5px] text-text-faint transition-colors hover:border-text-faint hover:text-text-muted"
            >
              {copied ? <Check size={11} /> : <Copy size={11} />}
              <span>Reference: {error.requestId}</span>
            </button>
          )}

          {(onRetry || onDismiss || (actions && actions.length > 0)) && (
            <div className="mt-2.5 flex flex-wrap gap-2">
              {onRetry && (
                <button
                  type="button"
                  onClick={onRetry}
                  className="inline-flex items-center gap-1.5 rounded-[3px] bg-rust/90 px-2.5 py-1.5 text-[11.5px] font-medium text-ink transition-colors hover:bg-rust"
                >
                  <RotateCcw size={11} />
                  Try again
                </button>
              )}
              {actions?.map((action) => (
                <button
                  key={action.label}
                  type="button"
                  onClick={action.onClick}
                  className="rounded-[3px] border border-border-soft px-2.5 py-1.5 text-[11.5px] font-medium text-text-muted transition-colors hover:border-text-faint hover:text-text"
                >
                  {action.label}
                </button>
              ))}
              {onDismiss && (
                <button
                  type="button"
                  onClick={onDismiss}
                  className="rounded-[3px] px-2.5 py-1.5 text-[11.5px] font-medium text-text-faint transition-colors hover:text-text-muted"
                >
                  Dismiss
                </button>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
