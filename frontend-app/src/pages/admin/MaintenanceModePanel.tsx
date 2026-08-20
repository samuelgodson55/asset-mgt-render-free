// Admin panel (React SPA) for the site-wide maintenance-mode toggle -- the
// UI counterpart to backend/api/maintenance_api.py. Only ever mounted for
// the true root Super Admin (see the surrounding Admin.tsx tab guard),
// since toggling this can lock every other user out of the app.
import { useEffect, useState } from "react";
import { AlertTriangle, Power, Wrench } from "lucide-react";
import { maintenanceApi } from "../../lib/api";
import { ErrorBanner } from "../../components/ui/ErrorBanner";

// Kept in sync with backend/schemas/maintenance_schema.py's own default so
// the textarea shows sensible placeholder copy before the real status has
// loaded, rather than an empty box.
const DEFAULT_MESSAGE = "We are currently performing scheduled maintenance. Please check back shortly.";

export function MaintenanceModePanel() {
  const [enabled, setEnabled] = useState(false);
  const [message, setMessage] = useState(DEFAULT_MESSAGE);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Separate from `busy` -- gates the initial "Loading…" skeleton on
  // first mount only, so a later save-in-progress doesn't re-trigger it.
  const [loaded, setLoaded] = useState(false);

  const load = async () => {
    try {
      const s = await maintenanceApi.status();
      setEnabled(s.enabled);
      setMessage(s.message);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unable to load maintenance status");
    } finally {
      setLoaded(true);
    }
  };

  useEffect(() => {
    load();
  }, []);

  const save = async (next: boolean) => {
    // A confirm() dialog here on purpose -- this is one of the few admin
    // actions that instantly locks out every other signed-in user, so a
    // misclick shouldn't be one accidental button press away.
    const question = next
      ? "Enable maintenance mode? Other users will immediately be blocked from the application."
      : "Disable maintenance mode and restore normal access for users?";
    if (!window.confirm(question)) return;

    setBusy(true);
    setError(null);
    try {
      const s = await maintenanceApi.update({ enabled: next, message });
      setEnabled(s.enabled);
      setMessage(s.message);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unable to update maintenance mode");
    } finally {
      setBusy(false);
    }
  };

  if (!loaded) {
    return (
      <section className="rounded-xl border border-border bg-surface p-5 text-sm text-text-muted">
        Loading maintenance settings…
      </section>
    );
  }

  return (
    // Panel border/background shifts to a "brass" accent while maintenance
    // is live, so this stays visually distinct from every other admin
    // panel while it's actually affecting production traffic.
    <section className={`rounded-xl border p-5 ${enabled ? "border-brass/40 bg-brass/[0.04]" : "border-border bg-surface"}`}>
      <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <Wrench size={19} className={enabled ? "text-brass" : "text-text-muted"} />
            <h2 className="font-semibold">Maintenance Mode</h2>
            {enabled ? (
              <span className="rounded-full border border-brass/30 bg-brass/10 px-2 py-0.5 text-[10px] font-bold tracking-wide text-brass">
                LIVE
              </span>
            ) : null}
          </div>
          <p className="mt-2 max-w-2xl text-sm text-text-muted">
            Temporarily blocks every user except the root Super Admin. Use this for planned maintenance and deployments.
          </p>
        </div>
        <button
          disabled={busy}
          onClick={() => save(!enabled)}
          className={`inline-flex items-center justify-center gap-2 rounded-lg px-4 py-2 text-sm font-semibold disabled:opacity-60 ${
            enabled ? "bg-foreground text-background" : "border border-brass/40 text-brass hover:bg-brass/10"
          }`}
        >
          <Power size={16} />
          {busy ? "Saving…" : enabled ? "Disable Maintenance" : "Enable Maintenance"}
        </button>
      </div>
      {error ? (
        <div className="mt-4">
          <ErrorBanner>{error}</ErrorBanner>
        </div>
      ) : null}
      <div className="mt-5">
        <label className="mb-2 block text-xs font-semibold uppercase tracking-wide text-text-muted">
          Maintenance message
        </label>
        <textarea
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          maxLength={500}
          rows={3}
          className="w-full rounded-lg border border-border bg-ink px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-brass"
        />
        <p className="mt-2 flex items-center gap-2 text-xs text-text-muted">
          <AlertTriangle size={13} />
          Changes to the message are applied when maintenance is enabled or disabled.
        </p>
      </div>
    </section>
  );
}
