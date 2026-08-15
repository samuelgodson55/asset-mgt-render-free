import { useEffect, useMemo, useState } from "react";
import { motion } from "framer-motion";
import { UserRound, KeyRound, ShieldCheck, Download, Check } from "lucide-react";
import { profileApi, ApiError } from "../lib/api";
import { useAuth } from "../lib/useAuth";
import { useRequestGuard } from "../lib/useRequestGuard";
import { isTrueSuperAdmin } from "../lib/roles";
import type { ProfileDetail } from "../lib/types";

type ProfileTab = "account" | "security" | "two-factor";

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
    <div className="border border-border-soft bg-surface rounded-[3px] p-5 max-w-3xl">
      <div className="flex items-start gap-3 mb-5">
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

function UpdateIdentityCard({ profile, onUpdated }: { profile: ProfileDetail; onUpdated: (p: ProfileDetail) => void }) {
  const [name, setName] = useState(profile.name);
  const [email, setEmail] = useState(profile.email);
  const [username, setUsername] = useState(profile.username || "");
  const [phoneNumber, setPhoneNumber] = useState(profile.phone_number || "");
  const [company, setCompany] = useState(profile.company || "");
  const [password, setPassword] = useState("");
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setMsg(null);
    setSubmitting(true);
    try {
      const res = await profileApi.updateIdentity(name.trim(), email.trim(), username.trim(), password, phoneNumber.trim(), company.trim());
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
    <div className="border border-border-soft bg-surface rounded-[3px] p-5 max-w-3xl">
      <div className="flex items-start gap-3 mb-5">
        <div className="w-9 h-9 rounded-full bg-brass/10 flex items-center justify-center shrink-0">
          <UserRound size={16} className="text-brass-soft" />
        </div>
        <div>
          <h2 className="font-display text-[15px] font-medium text-text">Account details</h2>
          <p className="text-[12.5px] text-text-muted mt-0.5">Update your personal account information. Re-enter your current password to confirm any change.</p>
        </div>
      </div>
      <form onSubmit={submit} className="flex flex-col gap-3">
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Name" className="bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none transition-colors" />
        <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="Email" className="bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none transition-colors" />
        <input value={username} onChange={(e) => setUsername(e.target.value)} placeholder="Username" className="bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none transition-colors" />
        <input type="tel" autoComplete="tel" value={phoneNumber} onChange={(e) => setPhoneNumber(e.target.value)} placeholder="Phone number" className="bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none transition-colors" />
        <input value={company} onChange={(e) => setCompany(e.target.value)} placeholder="Company" className="bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none transition-colors" />
        <input type="password" required autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="Current password" className="bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none transition-colors" />
        <button type="submit" disabled={submitting} className="bg-brass hover:bg-brass-soft disabled:opacity-60 text-ink font-medium text-[13px] rounded-[3px] py-2.5 transition-colors">
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
    <div className="border border-border-soft bg-surface rounded-[3px] p-5 max-w-3xl">
      <div className="flex items-start gap-3 mb-5">
        <div className="w-9 h-9 rounded-full bg-moss/10 flex items-center justify-center shrink-0">
          <ShieldCheck size={16} className="text-moss-soft" />
        </div>
        <div>
          <h2 className="font-display text-[15px] font-medium text-text">2FA recovery codes</h2>
          <p className="text-[12.5px] text-text-muted mt-0.5">Regenerating invalidates every old code. Only the newly generated batch will work.</p>
        </div>
      </div>

      {!codes ? (
        <form onSubmit={submit} className="flex flex-col gap-3">
          <input type="password" required autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="Current password" className="bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text placeholder:text-text-faint focus:border-moss/50 focus:outline-none transition-colors" />
          <button type="submit" disabled={submitting} className="bg-moss/90 hover:bg-moss disabled:opacity-60 text-white font-medium text-[13px] rounded-[3px] py-2.5 transition-colors">
            {submitting ? "Regenerating…" : "Regenerate recovery codes"}
          </button>
          {msg && <FieldMessage text={msg.text} ok={msg.ok} />}
        </form>
      ) : (
        <div>
          <div className="rounded-[3px] border border-moss/30 bg-moss/5 p-4">
            <div className="flex items-center gap-2 text-moss-soft text-[12px] font-medium mb-3">
              <Check size={14} /> New recovery codes generated
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
              {codes.map((code) => <code key={code} className="rounded-[3px] border border-border-soft bg-ink-soft px-3 py-2 text-[12px] text-text font-mono">{code}</code>)}
            </div>
          </div>
          <button type="button" onClick={download} className="mt-3 inline-flex items-center gap-2 border border-border-soft px-3 py-2 rounded-[3px] text-[12px] font-medium text-text-muted hover:text-text hover:border-border transition-colors">
            {downloaded ? <Check size={13} /> : <Download size={13} />}
            {downloaded ? "Downloaded" : "Download codes"}
          </button>
        </div>
      )}
    </div>
  );
}

export function Profile() {
  const { user, demo } = useAuth();
  const [profile, setProfile] = useState<ProfileDetail | null>(null);
  const [tab, setTab] = useState<ProfileTab>("account");
  const beginRequest = useRequestGuard();

  useEffect(() => {
    const isCurrent = beginRequest();
    profileApi.get().then((data) => { if (isCurrent()) setProfile(data); }).catch(() => { if (isCurrent()) setProfile(null); });
  }, [beginRequest]);

  const tabs = useMemo(() => {
    const list: Array<{ key: ProfileTab; label: string; icon: typeof UserRound }> = [
      { key: "account", label: "Account Details", icon: UserRound },
      { key: "security", label: "Security", icon: KeyRound },
    ];
    if (isTrueSuperAdmin(user?.role)) {
      list.push({ key: "two-factor", label: "2FA & Recovery", icon: ShieldCheck });
    }
    return list;
  }, [user?.role]);

  useEffect(() => {
    if (!tabs.some((item) => item.key === tab)) setTab(tabs[0]?.key ?? "account");
  }, [tabs, tab]);

  if (demo) {
    return (
      <div>
        <h1 className="font-display text-2xl font-semibold text-text mb-2">Profile</h1>
        <p className="text-text-muted text-sm">Profile management needs a real signed-in session -- not available in demo mode.</p>
      </div>
    );
  }

  const roleLabel = profile?.department_role || profile?.role || "Loading…";

  return (
    <div>
      <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }} className="mb-6 flex items-start justify-between gap-3 flex-wrap">
        <div>
          <h1 className="font-display text-2xl font-semibold text-text">My Profile</h1>
          <p className="text-text-muted text-sm mt-1">
            {profile ? `${profile.name} · ${roleLabel}` : "Loading…"}
          </p>
        </div>
      </motion.div>

      <div className="flex items-center gap-2 mb-5 flex-wrap">
        {tabs.map((item) => (
          <button
            key={item.key}
            type="button"
            onClick={() => setTab(item.key)}
            className={`flex items-center gap-1.5 px-3 py-1.5 rounded-full text-[12px] font-medium border transition-colors ${
              tab === item.key
                ? "bg-brass/15 border-brass/40 text-brass-soft"
                : "border-border-soft text-text-muted hover:text-text hover:border-border"
            }`}
          >
            <item.icon size={12} /> {item.label}
          </button>
        ))}
      </div>

      <motion.div key={tab} initial={{ opacity: 0, y: 5 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.2 }}>
        {tab === "account" && profile && <UpdateIdentityCard profile={profile} onUpdated={setProfile} />}
        {tab === "security" && profile && <ChangePasswordCard userId={profile.id} />}
        {tab === "two-factor" && isTrueSuperAdmin(user?.role) && <RecoveryCodesCard />}
      </motion.div>
    </div>
  );
}
