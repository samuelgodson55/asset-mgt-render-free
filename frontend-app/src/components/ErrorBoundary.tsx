import { Component, type ErrorInfo, type ReactNode } from "react";
import { RefreshCw, TriangleAlert } from "lucide-react";
import { reportClientError } from "../lib/errorbeacon";

interface Props {
  children: ReactNode;
}

interface State {
  hasError: boolean;
}

// Top-level safety net for uncaught React render errors.
//
// WITHOUT this, React 18's documented behavior on an uncaught render error
// is to unmount the ENTIRE tree -- #root goes empty, the user sees a blank
// white page with no explanation. The error still reaches ErrorBeacon (it
// re-throws as a genuine uncaught exception, which window.onerror in
// lib/errorbeacon.ts's installGlobalErrorBeacon() already catches), so
// reporting was never the gap -- the user-facing experience was.
//
// This does two things a bare window.onerror listener can't:
//   1. Renders a fallback instead of a blank page, so a broken component
//      doesn't take the whole app down with it.
//   2. Captures `info.componentStack` in componentDidCatch -- which
//      component actually broke, and where in the tree -- context a
//      window-level listener never has access to. Reported as its own
//      event (source: "ErrorBoundary") rather than relying on the
//      window.onerror catch to have equivalent detail.
//
// Deliberately mounted ONCE, at the very top of main.tsx, outside every
// other provider (Auth/Theme/Custody/QuoteDetail) -- so a crash in any of
// THOSE is caught too, not just crashes inside <App />. It intentionally
// has no dependency on any app context, since a provider crashing is
// exactly one of the cases it needs to survive.
export class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false };

  static getDerivedStateFromError(): State {
    return { hasError: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    reportClientError(error, {
      source: "ErrorBoundary",
      componentStack: info.componentStack?.slice(0, 4000),
    });
  }

  private handleReload = (): void => {
    window.location.reload();
  };

  render(): ReactNode {
    if (!this.state.hasError) return this.props.children;

    return (
      <div className="min-h-screen flex items-center justify-center bg-ink px-6">
        <div className="flex flex-col items-center text-center max-w-sm">
          <div className="flex items-center justify-center w-10 h-10 rounded-full bg-rust/10 border border-rust/50 mb-4">
            <TriangleAlert size={18} className="text-rust-soft" />
          </div>
          <h1 className="text-[15px] font-semibold text-text mb-1.5">Something went wrong</h1>
          <p className="text-[13px] text-text-muted mb-6">
            The app hit an unexpected error. This has already been reported -- try reloading the page.
          </p>
          <button
            onClick={this.handleReload}
            className="flex items-center gap-1.5 px-4 py-2 rounded-full text-[12px] font-medium border border-border-soft text-text-muted hover:text-text hover:border-border transition-colors"
          >
            <RefreshCw size={12} /> Reload
          </button>
        </div>
      </div>
    );
  }
}
