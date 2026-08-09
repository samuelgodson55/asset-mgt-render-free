import { useEffect, useState } from "react";
import { motion } from "framer-motion";
import { UserRound, KeyRound, ShieldCheck, Download, Check } from "lucide-react";
import { profileApi, ApiError } from "../lib/api";
import { useAuth } from "../lib/useAuth";
import { isFullAdmin, isTrueSuperAdmin } from "../lib/roles";
import type { ProfileDetail } from "../lib/types";

function errMsg(err: unknown, fallback: string): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error && err.message) return err.message;
  return fallback;
}

function FieldMessage({ text, ok }: { text: string; ok: boolean }) {
  return <p className={`text-[12px] mt-2 font-medium ${ok ? "text-moss-soft" : "text-rust-soft"}`}>{text}</p>;
}

function ChangePasswordCard({ userId }: { userId: number }) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setMsg(null);
    if (next !== confirm) {
      setMsg({ text: "New password and confirmation do not match.", ok: false });
      return;
    }
    setSubmitting(true);
    try {
      const res = await profileApi.updatePassword(userId, current, next);
      setMsg({ text: res.message || "Password updated successfully.", ok: true });
      setCurrent("");
      setNext("");
      setConfirm("");
    } catch (err) {
      setMsg({ text: errMsg(err, "Couldn't update your password."), ok: false });
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="border border-border-soft bg-surface rounded-[3px] p-5">
      <div className="flex items-start gap-3 mb-4">
        <div className="w-9 h-9 rounded-full bg-brass/10 flex items-center justify-center shrink-0">
          <KeyRound size={16} className="text-brass-soft" />
        </div>
        <div>
          <h2 className="font-display text-[15px] font-medium text-text">Change password</h2>
          <p className="text-[12.5px] text-text-muted mt-0.5">You'll need your current password to confirm this.</p>
        </div>
      </div>
      <form onSubmit={submit} className="flex flex-col gap-3">
        <input type="password" required autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} placeholder="Current password" className="bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none transition-colors" />
        <input type="password" required autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} placeholder="New password" className="bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none transition-colors" />
        <input type="password" required autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} placeholder="Confirm new password" className="bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none transition-colors" />
        <button type="submit" disabled={submitting} className="bg-brass hover:bg-brass-soft disabled:opacity-60 text-ink font-medium text-[13px] rounded-[3px] py-2.5 transition-colors">
          {submitting ? "Updating…" : "Update password"}
        </button>
        {msg && <FieldMessage text={msg.text} ok={msg.ok} />}
      </form>
    </div>
  );
}

// Available to every role on the backend (PATCH /auth/me is deliberately
// self-only, see services/auth_service.py's update_identity() docstring),
// but the legacy admin.html only ever surfaced it for the Super Admin's own
// identity section -- every other role's name/email/username can instead
// be fixed by a Super Admin OR a plain Admin via the User Directory's Edit
// action. Mirrored here for BOTH full-admin tiers (not just the root
// account) since a plain Admin has that same "nobody above me to fix it"
// gap for their OWN row -- another Admin can edit them, but self-service
// is still worth having, same as the Super Admin gets it.
function UpdateIdentityCard({ profile, onUpdated }: { profile: ProfileDetail; onUpdated: (p: ProfileDetail) => void }) {
  const [name, setName] = useState(profile.name);
  const [email, setEmail] = useState(profile.email);
  const [username, setUsername] = useState(profile.username || "");
  const [password, setPassword] = useState("");
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setMsg(null);
    setSubmitting(true);
    try {
      const res = await profileApi.updateIdentity(name.trim(), email.trim(), username.trim(), password);
      onUpdated(res);
      setMsg({ text: res.message || "Profile updated successfully.", ok: true });
      setPassword("");
    } catch (err) {
      setMsg({ text: errMsg(err, "Couldn't update your profile."), ok: false });
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="border border-border-soft bg-surface rounded-[3px] p-5">
      <div className="flex items-start gap-3 mb-4">
        <div className="w-9 h-9 rounded-full bg-sky/10 flex items-center justify-center shrink-0">
          <UserRound size={16} className="text-sky" />
        </div>
        <div>
          <h2 className="font-display text-[15px] font-medium text-text">Account details</h2>
          <p className="text-[12.5px] text-text-muted mt-0.5">Re-enter your password to confirm any change.</p>
        </div>
      </div>
      <form onSubmit={submit} className="flex flex-col gap-3">
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Name" className="bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none transition-colors" />
        <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="Email" className="bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none transition-colors" />
        <input value={username} onChange={(e) => setUsername(e.target.value)} placeholder="Username" className="bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none transition-colors" />
        <input type="password" required autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="Current password" className="bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none transition-colors" />
        <button type="submit" disabled={submitting} className="bg-sky/90 hover:bg-sky disabled:opacity-60 text-white font-medium text-[13px] rounded-[3px] py-2.5 transition-colors">
          {submitting ? "Saving…" : "Save changes"}
        </button>
        {msg && <FieldMessage text={msg.text} ok={msg.ok} />}
      </form>
    </div>
  );
}

function RecoveryCodesCard() {
  const [password, setPassword] = useState("");
  const [codes, setCodes] = useState<string[] | null>(null);
  const [downloaded, setDownloaded] = useState(false);
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setMsg(null);
    setSubmitting(true);
    try {
      const res = await profileApi.regenerateRecoveryCodes(password);
      setCodes(res.recovery_codes);
      setPassword("");
    } catch (err) {
      setMsg({ text: errMsg(err, "Couldn't regenerate recovery codes."), ok: false });
    } finally {
      setSubmitting(false);
    }
  };

  const download = () => {
    if (!codes) return;
    const text = ["Ledger -- 2FA recovery codes (regenerated)", "Each code works ONCE.", "", ...codes].join("\n");
    const blob = new Blob([text], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "ledger-recovery-codes.txt";
    a.click();
    URL.revokeObjectURL(url);
    setDownloaded(true);
  };

  return (
    <div className="border border-border-soft bg-surface rounded-[3px] p-5">
      <div className="flex items-start gap-3 mb-4">
        <div className="w-9 h-9 rounded-full bg-moss/10 flex items-center justify-center shrink-0">
          <ShieldCheck size={16} className="text-moss-soft" />
        </div>
        <div>
          <h2 className="font-display text-[15px] font-medium text-text">2FA recovery codes</h2>
          <p className="text-[12.5px] text-text-muted mt-0.5">Regenerating invalidates every old code -- only the new batch shown below will work.</p>
        </div>
      </div>

      {codes ? (
        <>
          <div className="grid grid-cols-2 gap-2 bg-ink-soft border border-border-soft rounded-[3px] p-4 mb-3 font-mono text-[12px] text-text">
            {codes.map((c) => <span key={c} className="select-all">{c}</span>)}
          </div>
          <button onClick={download} className="w-full flex items-center justify-center gap-2 border border-border-soft hover:border-brass/50 text-text text-[13px] rounded-[3px] py-2.5 transition-colors">
            {downloaded ? <Check size={13} /> : <Download size={13} />}
            {downloaded ? "Downloaded" : "Download as .txt"}
          </button>
        </>
      ) : (
        <form onSubmit={submit} className="flex flex-col gap-3">
          <input type="password" required autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="Current password" className="bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text placeholder:text-text-faint focus:border-moss/50 focus:outline-none transition-colors" />
          <button type="submit" disabled={submitting} className="bg-moss/90 hover:bg-moss disabled:opacity-60 text-white font-medium text-[13px] rounded-[3px] py-2.5 transition-colors">
            {submitting ? "Regenerating…" : "Regenerate recovery codes"}
          </button>
          {msg && <FieldMessage text={msg.text} ok={msg.ok} />}
        </form>
      )}
    </div>
  );
}

export function Profile() {
  const { user, demo } = useAuth();
  const [profile, setProfile] = useState<ProfileDetail | null>(null);

  useEffect(() => {
    profileApi.get().then(setProfile).catch(() => setProfile(null));
  }, []);

  if (demo) {
    return (
      <div>
        <h1 className="font-display text-2xl font-semibold text-text mb-2">Profile</h1>
        <p className="text-text-muted text-sm">Profile management needs a real signed-in session -- not available in demo mode.</p>
      </div>
    );
  }

  return (
    <div>
      <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }} className="mb-6">
        <h1 className="font-display text-2xl font-semibold text-text">My Profile</h1>
        <p className="text-text-muted text-sm mt-1">
          {profile ? `${profile.name} · ${profile.department_role || profile.role}` : "Loading…"}
        </p>
      </motion.div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 max-w-3xl">
        {profile && <ChangePasswordCard userId={profile.id} />}
        {profile && isFullAdmin(user?.role) && <UpdateIdentityCard profile={profile} onUpdated={setProfile} />}
        {/* 2FA is Super-Admin-only end to end (see services/auth_service.py's
            login() -- only role == SUPER_ADMIN_ROLE ever gets an mfa_required/
            mfa_setup_required challenge in the first place), and
            regenerate_recovery_codes() rejects anyone else server-side too --
            so a plain Admin, despite being full-admin-equivalent everywhere
            else, never sees this card. */}
        {isTrueSuperAdmin(user?.role) && <RecoveryCodesCard />}
      </div>
    </div>
  );
}
