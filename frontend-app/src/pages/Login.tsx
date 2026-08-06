import { useEffect, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { ArrowRight, Lock, AlertCircle, ShieldCheck, KeyRound, Download, Check, ScanLine, Boxes, Radar } from "lucide-react";
import { useNavigate } from "react-router-dom";
import QRCode from "qrcode";
import { useAuth } from "../lib/auth";
import { ApiError } from "../lib/api";
import { ThemeToggle } from "../components/ThemeToggle";

const CARD_TRANSITION = { duration: 0.35, ease: [0.16, 1, 0.3, 1] as const };

const STEPS = [
  { key: "login", label: "Sign in" },
  { key: "mfa", label: "Verify" },
  { key: "recovery", label: "Done" },
] as const;

function Brand({ compact = false }: { compact?: boolean }) {
  return (
    <div className={`flex items-center gap-2.5 ${compact ? "" : "mb-8"}`}>
      <svg width="22" height="22" viewBox="0 0 32 32">
        <path d="M4 4h14l10 10-14 14L4 18z" fill="#C89B3C" />
        <circle cx="9" cy="9" r="2.4" fill="#0F1219" />
      </svg>
      <div>
        <p className="font-display font-semibold text-text leading-none">Ledger</p>
        <p className="text-[10px] text-text-faint mt-0.5">Asset Management</p>
      </div>
    </div>
  );
}

function ErrorBanner({ message }: { message: string }) {
  return (
    <div className="flex items-start gap-2 bg-rust/10 border border-rust/30 text-rust-soft text-[12px] rounded-[3px] px-3 py-2.5">
      <AlertCircle size={13} className="mt-0.5 shrink-0" />
      <span>{message}</span>
    </div>
  );
}

function errorMessage(err: unknown, fallback: string): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error && err.message) return err.message;
  return fallback;
}

/** Small step tracker shown above the form on every screen -- "where am I in this flow" at a glance instead of only inferring it from the copy. */
function StepIndicator({ active }: { active: (typeof STEPS)[number]["key"] }) {
  const activeIndex = STEPS.findIndex((s) => s.key === active);
  return (
    <div className="flex items-center gap-1.5 mb-7">
      {STEPS.map((s, i) => (
        <div key={s.key} className="flex items-center gap-1.5">
          <div
            className={`h-[3px] rounded-full transition-all duration-300 ${
              i <= activeIndex ? "w-6 bg-brass" : "w-3 bg-border"
            }`}
          />
        </div>
      ))}
      <span className="ml-1.5 text-[10px] uppercase tracking-wider text-text-faint">{STEPS[Math.max(activeIndex, 0)].label}</span>
    </div>
  );
}

/** POST /auth/login form -- plain identifier/password, no 2FA state involved. */
function LoginForm({ onSwitchToDemo }: { onSwitchToDemo: () => void }) {
  const [identifier, setIdentifier] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const { login } = useAuth();

  return (
    <>
      <StepIndicator active="login" />
      <h1 className="font-display text-[26px] font-semibold text-text leading-tight">Sign in to the ledger</h1>
      <p className="text-[13.5px] text-text-muted mt-2 mb-7">Every checkout, tagged and tracked.</p>

      <form
        onSubmit={async (e) => {
          e.preventDefault();
          setError(null);
          setSubmitting(true);
          try {
            await login(identifier, password);
          } catch (err) {
            setError(errorMessage(err, "Couldn't reach the server. Try again."));
          } finally {
            setSubmitting(false);
          }
        }}
        className="flex flex-col gap-4"
      >
        <label className="block">
          <span className="text-[11px] uppercase tracking-wider text-text-faint">Email or Username</span>
          <input
            type="text"
            autoComplete="username"
            value={identifier}
            onChange={(e) => setIdentifier(e.target.value)}
            placeholder="you@organization.com or username"
            className="w-full mt-1.5 bg-ink-soft border border-border-soft rounded-[4px] px-3.5 py-3 text-[13.5px] text-text placeholder:text-text-faint focus:border-brass focus:ring-2 focus:ring-brass/15 focus:outline-none transition-all"
          />
        </label>
        <label className="block">
          <span className="text-[11px] uppercase tracking-wider text-text-faint">Password</span>
          <input
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="••••••••"
            className="w-full mt-1.5 bg-ink-soft border border-border-soft rounded-[4px] px-3.5 py-3 text-[13.5px] text-text placeholder:text-text-faint focus:border-brass focus:ring-2 focus:ring-brass/15 focus:outline-none transition-all"
          />
        </label>

        {error && <ErrorBanner message={error} />}

        <button
          type="submit"
          disabled={submitting}
          className="mt-2 flex items-center justify-center gap-2 bg-gradient-to-b from-brass-soft to-brass hover:brightness-110 disabled:opacity-60 text-ink font-semibold text-[13.5px] rounded-[4px] py-3 transition-all shadow-[0_1px_0_0_rgba(255,255,255,0.25)_inset,0_8px_20px_-10px_var(--color-brass)] group"
        >
          <Lock size={13} />
          {submitting ? "Signing in…" : "Sign in"}
          <ArrowRight size={13} className="group-hover:translate-x-0.5 transition-transform" />
        </button>
      </form>

      <div className="flex items-center gap-3 my-6">
        <div className="h-px flex-1 bg-border-soft" />
        <span className="text-[10px] uppercase tracking-wider text-text-faint">or</span>
        <div className="h-px flex-1 bg-border-soft" />
      </div>

      <button
        type="button"
        onClick={onSwitchToDemo}
        className="w-full flex items-center justify-center gap-2 border border-border-soft hover:border-sky/50 hover:bg-sky/5 text-text text-[12.5px] font-medium rounded-[4px] py-2.5 transition-colors"
      >
        <Radar size={13} className="text-sky" />
        Continue with demo data
      </button>
      <p className="text-[11px] text-text-faint text-center mt-3">Real sign-in hits the live backend; demo data needs no account.</p>
    </>
  );
}

/** POST /auth/mfa/verify screen -- already-enrolled super_admin. Accepts either a live TOTP code or an XXXXX-XXXXX recovery code, same field either way (the backend tells them apart by shape). */
function MfaVerifyScreen() {
  const [code, setCode] = useState("");
  const [recoveryMode, setRecoveryMode] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const { verifyMfaCode, cancelMfa } = useAuth();

  return (
    <>
      <StepIndicator active="mfa" />
      <div className="flex items-center gap-2 text-brass-soft mb-2">
        <ShieldCheck size={16} />
        <span className="text-[11px] uppercase tracking-wider">Two-factor verification</span>
      </div>
      <h1 className="font-display text-[26px] font-semibold text-text leading-tight">Enter your code</h1>
      <p className="text-[13.5px] text-text-muted mt-2 mb-7">
        {recoveryMode
          ? "Enter one of the unused recovery codes you saved when you set up 2FA."
          : "Open your authenticator app and enter the 6-digit code for this account."}
      </p>

      <form
        onSubmit={async (e) => {
          e.preventDefault();
          setError(null);
          setSubmitting(true);
          try {
            await verifyMfaCode(code.trim());
          } catch (err) {
            setError(errorMessage(err, "Incorrect code. Please try again."));
            setCode("");
          } finally {
            setSubmitting(false);
          }
        }}
        className="flex flex-col gap-4"
      >
        <label className="block">
          <span className="text-[11px] uppercase tracking-wider text-text-faint">
            {recoveryMode ? "Recovery code" : "Authenticator code"}
          </span>
          <input
            type="text"
            autoComplete="one-time-code"
            autoFocus
            value={code}
            onChange={(e) => setCode(e.target.value)}
            placeholder={recoveryMode ? "XXXXX-XXXXX" : "123456"}
            className="w-full mt-1.5 bg-ink-soft border border-border-soft rounded-[4px] px-3.5 py-3 text-[15px] tracking-[0.2em] text-text placeholder:text-text-faint placeholder:tracking-normal focus:border-brass focus:ring-2 focus:ring-brass/15 focus:outline-none transition-all font-mono"
          />
        </label>

        {error && <ErrorBanner message={error} />}

        <button
          type="submit"
          disabled={submitting || !code.trim()}
          className="mt-2 flex items-center justify-center gap-2 bg-gradient-to-b from-brass-soft to-brass hover:brightness-110 disabled:opacity-60 text-ink font-semibold text-[13.5px] rounded-[4px] py-3 transition-all shadow-[0_1px_0_0_rgba(255,255,255,0.25)_inset,0_8px_20px_-10px_var(--color-brass)] group"
        >
          <Lock size={13} />
          {submitting ? "Verifying…" : "Verify"}
          <ArrowRight size={13} className="group-hover:translate-x-0.5 transition-transform" />
        </button>
      </form>

      <button
        type="button"
        onClick={() => {
          setRecoveryMode((v) => !v);
          setCode("");
          setError(null);
        }}
        className="w-full text-center mt-5 text-[11.5px] text-text-muted hover:text-brass-soft transition-colors underline underline-offset-2 decoration-border-soft"
      >
        {recoveryMode ? "Use my authenticator app instead" : "Use a recovery code instead"}
      </button>
      <button
        type="button"
        onClick={cancelMfa}
        className="w-full text-center mt-2 text-[11.5px] text-text-faint hover:text-text-muted transition-colors"
      >
        Cancel and start over
      </button>
    </>
  );
}

/** POST /auth/mfa/setup/confirm screen -- first-time enrollment (or re-enrollment after a recovery code was used). Shows the secret + QR exactly once, per the backend's own guarantee. */
function MfaSetupScreen({ totpSecret, otpauthUri, message }: { totpSecret: string; otpauthUri: string; message?: string }) {
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [qrDataUrl, setQrDataUrl] = useState<string | null>(null);
  const { confirmMfaSetup, cancelMfa } = useAuth();

  useEffect(() => {
    let cancelled = false;
    // Reads the *current* semantic tokens (not fixed hex) so the QR tile
    // matches whichever theme is active instead of always rendering the
    // old dark-mode-only colors.
    const styles = getComputedStyle(document.documentElement);
    const dark = styles.getPropertyValue("--color-text").trim() || "#0F1219";
    const light = styles.getPropertyValue("--color-ink-soft").trim() || "#EFE7D4";
    QRCode.toDataURL(otpauthUri, { margin: 1, width: 200, color: { dark, light } })
      .then((url) => {
        if (!cancelled) setQrDataUrl(url);
      })
      .catch(() => {
        if (!cancelled) setQrDataUrl(null);
      });
    return () => {
      cancelled = true;
    };
  }, [otpauthUri]);

  return (
    <>
      <StepIndicator active="mfa" />
      <div className="flex items-center gap-2 text-brass-soft mb-2">
        <ShieldCheck size={16} />
        <span className="text-[11px] uppercase tracking-wider">Two-factor setup required</span>
      </div>
      <h1 className="font-display text-[26px] font-semibold text-text leading-tight">Set up your authenticator</h1>
      <p className="text-[13.5px] text-text-muted mt-2 mb-6">
        {message ||
          "This account requires 2FA. Add it to an authenticator app (Google Authenticator, Authy, 1Password, etc.), then confirm the code it shows below."}
      </p>

      <div className="flex justify-center bg-ink-soft border border-border-soft rounded-[4px] p-4 mb-4">
        {qrDataUrl ? (
          <img src={qrDataUrl} alt="Scan with your authenticator app" width={180} height={180} className="rounded-[2px]" />
        ) : (
          <p className="text-[11px] text-text-faint text-center py-8">QR code unavailable — use the manual entry key below instead.</p>
        )}
      </div>

      <label className="block mb-4">
        <span className="text-[11px] uppercase tracking-wider text-text-faint">Manual entry key</span>
        <div className="mt-1.5 bg-ink-soft border border-border-soft rounded-[4px] px-3.5 py-3 text-[12px] font-mono text-text-muted break-all select-all">
          {totpSecret}
        </div>
      </label>

      <form
        onSubmit={async (e) => {
          e.preventDefault();
          setError(null);
          setSubmitting(true);
          try {
            await confirmMfaSetup(code.trim());
          } catch (err) {
            setError(errorMessage(err, "Incorrect code. Please try again."));
            setCode("");
          } finally {
            setSubmitting(false);
          }
        }}
        className="flex flex-col gap-4"
      >
        <label className="block">
          <span className="text-[11px] uppercase tracking-wider text-text-faint">Code from your app</span>
          <input
            type="text"
            autoComplete="one-time-code"
            autoFocus
            value={code}
            onChange={(e) => setCode(e.target.value)}
            placeholder="123456"
            className="w-full mt-1.5 bg-ink-soft border border-border-soft rounded-[4px] px-3.5 py-3 text-[15px] tracking-[0.2em] text-text placeholder:text-text-faint placeholder:tracking-normal focus:border-brass focus:ring-2 focus:ring-brass/15 focus:outline-none transition-all font-mono"
          />
        </label>

        {error && <ErrorBanner message={error} />}

        <button
          type="submit"
          disabled={submitting || !code.trim()}
          className="mt-1 flex items-center justify-center gap-2 bg-gradient-to-b from-brass-soft to-brass hover:brightness-110 disabled:opacity-60 text-ink font-semibold text-[13.5px] rounded-[4px] py-3 transition-all shadow-[0_1px_0_0_rgba(255,255,255,0.25)_inset,0_8px_20px_-10px_var(--color-brass)] group"
        >
          <ShieldCheck size={13} />
          {submitting ? "Confirming…" : "Confirm and enable 2FA"}
          <ArrowRight size={13} className="group-hover:translate-x-0.5 transition-transform" />
        </button>
      </form>

      <button
        type="button"
        onClick={cancelMfa}
        className="w-full text-center mt-4 text-[11.5px] text-text-faint hover:text-text-muted transition-colors"
      >
        Cancel and start over
      </button>
    </>
  );
}

/** Shown exactly once, right after mfa/setup/confirm succeeds -- ten single-use backup codes for when the authenticator device is lost. */
function RecoveryCodesScreen({ codes }: { codes: string[] }) {
  const [downloaded, setDownloaded] = useState(false);
  const { dismissRecoveryCodes } = useAuth();

  const download = () => {
    const text = [
      "Ledger -- 2FA recovery codes",
      "Each code works ONCE. Store this file somewhere safe (a password manager, not your Downloads folder long-term).",
      "",
      ...codes,
    ].join("\n");
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
    <>
      <StepIndicator active="recovery" />
      <div className="flex items-center gap-2 text-moss-soft mb-2">
        <KeyRound size={16} />
        <span className="text-[11px] uppercase tracking-wider">Save your recovery codes</span>
      </div>
      <h1 className="font-display text-[26px] font-semibold text-text leading-tight">2FA is enabled</h1>
      <p className="text-[13.5px] text-text-muted mt-2 mb-6">
        Each code below works once, if you ever lose access to your authenticator app. They're shown only this one time — save them somewhere safe now.
      </p>

      <div className="grid grid-cols-2 gap-2 bg-ink-soft border border-border-soft rounded-[4px] p-4 mb-5 font-mono text-[12.5px] text-text">
        {codes.map((c) => (
          <span key={c} className="select-all">{c}</span>
        ))}
      </div>

      <button
        type="button"
        onClick={download}
        className="w-full flex items-center justify-center gap-2 border border-border-soft hover:border-brass/50 text-text text-[13px] rounded-[4px] py-3 transition-colors mb-3"
      >
        {downloaded ? <Check size={13} /> : <Download size={13} />}
        {downloaded ? "Downloaded" : "Download as .txt"}
      </button>

      <button
        type="button"
        onClick={dismissRecoveryCodes}
        className="w-full flex items-center justify-center gap-2 bg-gradient-to-b from-brass-soft to-brass hover:brightness-110 text-ink font-semibold text-[13.5px] rounded-[4px] py-3 transition-all shadow-[0_1px_0_0_rgba(255,255,255,0.25)_inset,0_8px_20px_-10px_var(--color-brass)] group"
      >
        Continue to the ledger
        <ArrowRight size={13} className="group-hover:translate-x-0.5 transition-transform" />
      </button>
    </>
  );
}

/** Left brand panel -- always the dark palette regardless of the active theme, so the app has one confident visual anchor rather than washing out to match light mode. Purely decorative; hidden below lg. */
function BrandPanel() {
  const tags = [
    { rot: -8, x: -120, y: -140, delay: 0.15, cat: "OPT-0114", label: "Vortex Diamondback HD" },
    { rot: 5, x: 140, y: -40, delay: 0.3, cat: "PWR-0087", label: "EcoFlow Delta Pro" },
    { rot: -5, x: -80, y: 160, delay: 0.45, cat: "NET-0203", label: "Cradlepoint IBR900" },
    { rot: 9, x: 150, y: 190, delay: 0.6, cat: "FAB-0031", label: "Prusa MK4S" },
  ];

  return (
    <div className="relative hidden lg:flex flex-col justify-between w-[46%] shrink-0 overflow-hidden bg-[#0F1219] px-12 py-12">
      <div
        className="absolute inset-0 opacity-[0.05]"
        style={{
          backgroundImage: "linear-gradient(#8B93A7 1px, transparent 1px), linear-gradient(90deg, #8B93A7 1px, transparent 1px)",
          backgroundSize: "44px 44px",
        }}
      />
      <div
        className="ambient-glow absolute top-[-10%] left-[10%] w-[520px] h-[520px] rounded-full pointer-events-none opacity-[0.18]"
        style={{ background: "radial-gradient(circle, #C89B3C 0%, transparent 70%)" }}
      />
      <div
        className="ambient-glow absolute bottom-[-15%] right-[-10%] w-[420px] h-[420px] rounded-full pointer-events-none opacity-[0.14]"
        style={{ background: "radial-gradient(circle, #6B93C8 0%, transparent 70%)", animationDelay: "-6s" }}
      />

      <div className="relative z-10">
        <svg width="26" height="26" viewBox="0 0 32 32">
          <path d="M4 4h14l10 10-14 14L4 18z" fill="#C89B3C" />
          <circle cx="9" cy="9" r="2.4" fill="#0F1219" />
        </svg>
      </div>

      <div className="relative z-10 pointer-events-none">
        {tags.map((t, i) => (
          <motion.div
            key={i}
            initial={{ opacity: 0, x: t.x, y: t.y + 24, rotate: t.rot }}
            animate={{ opacity: 0.9, y: t.y }}
            transition={{ duration: 1, delay: t.delay, ease: [0.16, 1, 0.3, 1] }}
            className="absolute left-1/2 top-0 tag-notch bg-[#1A1E28] border border-[#2A2F3E] px-4 py-2.5 pr-6 shadow-lg shadow-black/30"
          >
            <p className="font-mono text-[9.5px] text-[#E8C878]/80 tracking-widest">{t.cat}</p>
            <p className="text-[11px] text-[#EDEFF4] mt-0.5 whitespace-nowrap">{t.label}</p>
          </motion.div>
        ))}
      </div>

      <div className="relative z-10 max-w-sm">
        <div className="flex items-center gap-1.5 mb-4">
          <Boxes size={13} className="text-[#E8C878]" />
          <span className="text-[10.5px] uppercase tracking-widest text-[#E8C878]/80">Asset Management, Reimagined</span>
        </div>
        <h2 className="font-display text-[30px] leading-[1.15] font-semibold text-[#EDEFF4]">
          Know exactly<br />what's out, and<br />who has it.
        </h2>
        <p className="text-[13px] text-[#8B93A7] mt-4 leading-relaxed">
          One ledger for every pool, checkout, and overdue return — synced across your whole team, in real time.
        </p>
        <div className="flex items-center gap-5 mt-7 text-[11px] text-[#8B93A7]">
          <div className="flex items-center gap-1.5"><ScanLine size={12} className="text-[#7FBD9C]" /> Tag-level tracking</div>
          <div className="flex items-center gap-1.5"><ShieldCheck size={12} className="text-[#6B93C8]" /> 2FA secured</div>
        </div>
      </div>
    </div>
  );
}

export function Login() {
  const { user, mfaChallenge, recoveryCodes, continueAsDemo } = useAuth();
  const navigate = useNavigate();

  // Navigate once a real session actually exists -- covers plain login,
  // mfa/verify, and mfa/setup/confirm alike. Held back while recoveryCodes
  // is showing so that one-time screen isn't skipped past.
  useEffect(() => {
    if (user && !recoveryCodes) navigate("/");
  }, [user, recoveryCodes, navigate]);

  let screenKey = "login";
  if (recoveryCodes) screenKey = "recovery";
  else if (mfaChallenge?.kind === "verify") screenKey = "mfa-verify";
  else if (mfaChallenge?.kind === "setup") screenKey = "mfa-setup";

  return (
    <div className="min-h-screen flex bg-ink">
      <BrandPanel />

      <div className="relative flex-1 flex items-center justify-center px-4 py-10 overflow-hidden">
        <div
          className="absolute inset-0 opacity-[0.035] pointer-events-none"
          style={{
            backgroundImage: "linear-gradient(#8B93A7 1px, transparent 1px), linear-gradient(90deg, #8B93A7 1px, transparent 1px)",
            backgroundSize: "40px 40px",
          }}
        />

        <ThemeToggle className="absolute top-5 right-5 z-10" />
        <div className="absolute top-5 left-5 z-10 lg:hidden">
          <Brand compact />
        </div>

        <AnimatePresence mode="wait">
          <motion.div
            key={screenKey}
            initial={{ opacity: 0, y: 16 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -8 }}
            transition={CARD_TRANSITION}
            className="relative w-full max-w-sm"
          >
            {screenKey === "login" && (
              <LoginForm
                onSwitchToDemo={() => {
                  continueAsDemo();
                  navigate("/");
                }}
              />
            )}
            {screenKey === "mfa-verify" && <MfaVerifyScreen />}
            {screenKey === "mfa-setup" && mfaChallenge?.kind === "setup" && (
              <MfaSetupScreen
                totpSecret={mfaChallenge.totpSecret}
                otpauthUri={mfaChallenge.otpauthUri}
                message={mfaChallenge.message}
              />
            )}
            {screenKey === "recovery" && recoveryCodes && <RecoveryCodesScreen codes={recoveryCodes} />}
          </motion.div>
        </AnimatePresence>
      </div>
    </div>
  );
}
