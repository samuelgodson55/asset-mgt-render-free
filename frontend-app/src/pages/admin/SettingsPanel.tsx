// =============================================================================
// Settings -- Quotation Settings (VAT) ported from the legacy frontend's
// admin.html "Quotation Settings" card (js/components/quotation.js's
// loadVatSetting()/submitVatSettingsForm()). PUT /settings/vat is
// require_super_admin -- Super Admin AND a plain Admin account; GET is open
// to any authenticated user, but this panel itself is never shown outside
// Admin, so that distinction doesn't need its own gate here.
//
// Daily Digest Recipients lives here (rather than on System Backups) since
// that tab is gated require_true_super_admin on the frontend
// (isTrueSuperAdmin(), see canBackups in AdminOrManagerPage), but this
// list's own backend route is only require_super_admin (Super Admin AND a
// plain Admin account -- see backend/api/notifications_api.py), so a plain
// Admin could never reach it there even though they were always allowed to
// manage it. Settings is gated on canSettings (isFullAdmin -- Super Admin
// OR Admin), the correct audience for this list.
// =============================================================================
import { useEffect, useState } from "react";
import { Mail, Plus, X, Percent } from "lucide-react";
import { digestApi, quotationsApi } from "../../lib/api";
import { errMsg } from "./sharedHelpers";
import { useRequestGuard } from "../../lib/useRequestGuard";

// Lives on the Settings tab (Super Admin AND a plain Admin, via
// canSettings/isFullAdmin below) rather than the System Backups tab --
// System Backups itself is gated to the TRUE Super Admin only
// (canBackups/isTrueSuperAdmin), which would have hidden this list from a
// plain Admin even though the backend route behind it
// (GET/PUT /settings/digest-recipients) has always allowed a plain Admin,
// not just Super Admin -- see backend/api/notifications_api.py.
function DigestRecipients() {
  const [emails, setEmails] = useState<string[]>([]);
  const [input, setInput] = useState("");
  const [message, setMessage] = useState<{ text: string; tone: "ok" | "err" } | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    digestApi.list()
      .then((e) => { if (!cancelled) setEmails(e); })
      .catch((err) => { if (!cancelled) console.error("Failed to load digest recipients:", err); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, []);

  const save = async (next: string[], successMessage: string) => {
    try {
      const res = await digestApi.set(next);
      setEmails(res.emails ?? next);
      setMessage({ text: successMessage, tone: "ok" });
    } catch (err) {
      setMessage({ text: errMsg(err, "Couldn't save."), tone: "err" });
    }
  };

  const add = async (e: React.FormEvent) => {
    e.preventDefault();
    const email = input.trim().toLowerCase();
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) {
      setMessage({ text: "Enter a valid email address.", tone: "err" });
      return;
    }
    if (emails.includes(email)) {
      setMessage({ text: "That address is already on the list.", tone: "err" });
      return;
    }
    await save([...emails, email], `${email} will now receive the daily digest.`);
    setInput("");
  };

  const remove = (email: string) => save(emails.filter((e) => e !== email), `${email} removed from the daily digest.`);

  return (
    <div className="border border-border-soft bg-surface rounded-[3px] p-5">
      <div className="flex items-start gap-3 mb-4">
        <div className="w-9 h-9 rounded-full bg-sky/10 flex items-center justify-center shrink-0">
          <Mail size={16} className="text-sky" />
        </div>
        <div>
          <h2 className="font-display text-[15px] font-medium text-text">Daily digest recipients</h2>
          <p className="text-[12.5px] text-text-muted mt-0.5">The once-a-day overdue/due-soon summary email goes to these addresses only -- being an Admin or Manager no longer implies receiving it.</p>
        </div>
      </div>

      {loading ? (
        <p className="text-[12px] text-text-faint">Loading…</p>
      ) : (
        <div className="flex flex-wrap gap-2 mb-4">
          {emails.length === 0 && <span className="text-[12px] text-text-faint">No recipients configured -- the daily digest currently has nowhere to send.</span>}
          {emails.map((email) => (
            <span key={email} className="flex items-center gap-2 rounded-full border border-border-soft bg-surface-raised py-1 pl-3 pr-1.5 text-[12px] text-text">
              {email}
              <button onClick={() => remove(email)} title="Remove" className="rounded-full p-0.5 text-text-faint hover:bg-rust/10 hover:text-rust-soft transition-colors">
                <X size={11} />
              </button>
            </span>
          ))}
        </div>
      )}

      <form onSubmit={add} className="flex gap-2">
        <input
          type="email"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="name@organization.com"
          className="flex-1 bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2 text-[12.5px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none transition-colors"
        />
        <button type="submit" className="flex items-center gap-1.5 bg-brass hover:bg-brass-soft text-ink font-medium text-[12.5px] rounded-[3px] px-3 transition-colors">
          <Plus size={13} /> Add
        </button>
      </form>
      {message && <p className={`text-[12px] mt-2 font-medium ${message.tone === "ok" ? "text-moss-soft" : "text-rust-soft"}`}>{message.text}</p>}
    </div>
  );
}


export function SettingsPanel() {
  const [vatPercent, setVatPercent] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<{ text: string; ok: boolean } | null>(null);
  const beginRequest = useRequestGuard();

  useEffect(() => {
    const isCurrent = beginRequest();
    quotationsApi.getVat().then((data) => { if (isCurrent()) setVatPercent(String(data.vat_percent)); }).catch(() => {}).finally(() => { if (isCurrent()) setLoading(false); });
  }, [beginRequest]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const parsed = parseFloat(vatPercent);
    if (isNaN(parsed) || parsed < 0 || parsed > 100) {
      setMessage({ text: "Enter a VAT percentage between 0 and 100.", ok: false });
      return;
    }
    setMessage(null);
    setSaving(true);
    try {
      const data = await quotationsApi.setVat(parsed);
      setVatPercent(String(data.vat_percent));
      setMessage({ text: "VAT updated -- applies to every saved order immediately.", ok: true });
    } catch (err) {
      setMessage({ text: errMsg(err, "Couldn't update VAT."), ok: false });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="flex flex-col lg:flex-row lg:items-start gap-6">
      <div className="w-full lg:max-w-sm lg:shrink-0">
        <div className="border border-border-soft bg-surface rounded-[3px] p-5">
          <div className="flex items-center gap-2 mb-1">
            <Percent size={14} className="text-brass-soft" />
            <h2 className="font-display text-[14px] font-semibold text-text">Quotation Settings</h2>
          </div>
          <p className="text-[12px] text-text-muted mb-4">The global VAT percentage applied to every Quotation's total.</p>

          {loading ? (
            <p className="text-[12px] text-text-faint">Loading…</p>
          ) : (
            <form onSubmit={submit} className="flex flex-col gap-3">
              <label className="block">
                <span className="text-[11px] uppercase tracking-wider text-text-faint">VAT percent</span>
                <input
                  type="number"
                  min={0}
                  max={100}
                  step={0.01}
                  value={vatPercent}
                  onChange={(e) => setVatPercent(e.target.value)}
                  className="w-full mt-1.5 bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text focus:border-brass/50 focus:outline-none transition-colors"
                />
              </label>
              {message && (
                <p className={`text-[12px] font-medium ${message.ok ? "text-moss-soft" : "text-rust-soft"}`}>{message.text}</p>
              )}
              <button type="submit" disabled={saving} className="bg-brass hover:bg-brass-soft disabled:opacity-60 text-ink font-medium text-[13px] rounded-[3px] py-2.5 transition-colors">
                {saving ? "Saving…" : "Save VAT setting"}
              </button>
            </form>
          )}
        </div>
      </div>

      {/* Daily Digest Recipients -- moved here from the System Backups tab.
          That tab is gated require_true_super_admin on the frontend
          (isTrueSuperAdmin(), see canBackups below), but this list's own
          backend route is only require_super_admin (Super Admin AND a
          plain Admin account -- see backend/api/notifications_api.py), so
          a plain Admin could never reach it there even though they were
          always allowed to manage it. Settings is gated on canSettings
          (isFullAdmin -- Super Admin OR Admin, see below), the correct
          audience for this list.

          Sits beside the VAT card at `lg` and up (`flex-row` on the
          parent) so the wide Settings tab's spare horizontal room gets
          used instead of leaving this card stranded under a much
          narrower one; stacks back underneath it on smaller screens. */}
      <div className="w-full lg:flex-1 lg:max-w-xl">
        <DigestRecipients />
      </div>
    </div>
  );
}
