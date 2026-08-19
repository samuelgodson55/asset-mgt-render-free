import { useEffect, useState, type ReactNode } from "react";
import { useLocation } from "react-router-dom";
import { Wrench, RefreshCw, ShieldCheck } from "lucide-react";
import { quotationsApi } from "../lib/api";
import { useAuth } from "../lib/useAuth";
import { isTrueSuperAdmin } from "../lib/roles";

interface MaintenanceState {
  enabled: boolean;
  message: string;
  site: string;
}

const DEFAULT_MESSAGE = "We are currently performing scheduled maintenance. Please check back shortly.";

// NOTE: this component must render INSIDE <BrowserRouter> (see App.tsx) so
// `useLocation()` below reflects the current route on every navigation.
export function MaintenanceGate({ children }: { children: ReactNode }) {
  const { user, loading } = useAuth();
  const location = useLocation();
  const [state, setState] = useState<MaintenanceState>({ enabled: false, message: "", site: "" });
  const [checked, setChecked] = useState(false);

  const refresh = async () => {
    try {
      const c = await quotationsApi.publicConfig();
      setState({
        enabled: !!c.maintenance_mode,
        message: c.maintenance_message || DEFAULT_MESSAGE,
        site: c.site_name || "",
      });
    } finally {
      setChecked(true);
    }
  };

  useEffect(() => {
    refresh();
    const id = window.setInterval(refresh, 60000);
    const onVisible = () => {
      if (document.visibilityState === "visible") refresh();
    };
    const onMaintenance = () => refresh();
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("asset-app:maintenance", onMaintenance);
    return () => {
      clearInterval(id);
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("asset-app:maintenance", onMaintenance);
    };
  }, []);

  if (!checked || loading) return null;

  const bypass = isTrueSuperAdmin(user?.role);

  // `?maintenance_admin=1` is a UI-only escape hatch for the login screen --
  // it never grants authorization (the backend re-validates the real
  // Super Admin session on every request regardless). Deriving this fresh
  // from `location` on every render, instead of latching it into state once,
  // keeps it scoped to the login route: navigating anywhere else in the SPA
  // makes it fall away immediately rather than silently suppressing the
  // maintenance screen for the rest of the session on every route.
  const params = new URLSearchParams(location.search);
  const adminSignIn = location.pathname === "/login" && params.has("maintenance_admin");

  if (state.enabled && !bypass && !adminSignIn) {
    return (
      <main className="min-h-screen flex items-center justify-center bg-ink p-6">
        <section className="w-full max-w-xl rounded-2xl border border-border bg-surface p-8 text-center shadow-2xl">
          <div className="mx-auto mb-5 flex h-16 w-16 items-center justify-center rounded-2xl border border-brass/30 bg-brass/10 text-brass">
            <Wrench size={30} />
          </div>
          <div className="mb-3 inline-flex items-center gap-2 rounded-full border border-brass/25 bg-brass/10 px-3 py-1 text-xs font-semibold text-brass">
            <span className="h-2 w-2 animate-pulse rounded-full bg-brass" />
            Maintenance in progress
          </div>
          <h1 className="text-3xl font-semibold tracking-tight">We’re improving things</h1>
          <p className="mx-auto mt-4 max-w-md text-sm leading-6 text-text-muted">{state.message}</p>
          <p className="mt-4 text-xs text-text-muted">Your data remains safe and unchanged.</p>
          <button
            onClick={refresh}
            className="mt-7 inline-flex items-center gap-2 rounded-lg border border-border px-4 py-2 text-sm font-medium hover:bg-surface-raised"
          >
            <RefreshCw size={16} />
            Refresh status
          </button>
          <div className="mt-8 border-t border-border pt-5 text-xs text-text-muted">
            <a href="/login?maintenance_admin=1" className="inline-flex items-center gap-1 hover:text-text">
              <ShieldCheck size={14} />
              Administrator sign in
            </a>
            {state.site ? <div className="mt-4">{state.site}</div> : null}
          </div>
        </section>
      </main>
    );
  }

  return (
    <>
      {state.enabled && bypass ? (
        <div className="sticky top-0 z-[100] flex items-center justify-between gap-3 border-b border-amber-300 bg-amber-50 px-4 py-2 text-xs text-amber-950 dark:border-amber-700/60 dark:bg-amber-950/40 dark:text-amber-100">
          <span>
            <strong>Maintenance mode is active.</strong> Other users cannot access the application.
          </span>
          <a
            href="/admin?tab=maintenance"
            className="rounded-md border border-amber-400 bg-amber-100 px-2 py-1 font-medium text-amber-950 hover:bg-amber-200 dark:border-amber-700 dark:bg-amber-900/50 dark:text-amber-50 dark:hover:bg-amber-900"
          >
            Manage maintenance
          </a>
        </div>
      ) : null}
      {children}
    </>
  );
}
