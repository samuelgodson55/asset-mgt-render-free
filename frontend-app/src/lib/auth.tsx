import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { auth, type AuthUser } from "./api";

// Mirrors backend/services/auth_service.py's two 2FA challenge shapes --
// see README's "Two-factor authentication (2FA)" section. Only super_admin
// accounts ever produce one of these; every other role's login() resolves
// straight to a session.
export type MfaChallenge =
  | { kind: "setup"; token: string; totpSecret: string; otpauthUri: string; message?: string }
  | { kind: "verify"; token: string };

interface AuthContextValue {
  user: AuthUser | null;
  /** true while the initial GET /auth/me check (on load/refresh) is in flight */
  loading: boolean;
  /** true once the person has explicitly chosen to skip real sign-in and browse demo data instead */
  demo: boolean;
  /** set after login() returns mfa_required or mfa_setup_required -- Login.tsx renders the matching screen while this is non-null */
  mfaChallenge: MfaChallenge | null;
  /** shown exactly once, right after mfa/setup/confirm succeeds, before the person continues into the app */
  recoveryCodes: string[] | null;
  login: (identifier: string, password: string) => Promise<void>;
  /** submits a 6-digit TOTP code (or an XXXXX-XXXXX recovery code) against an mfaChallenge of kind "verify" */
  verifyMfaCode: (code: string) => Promise<void>;
  /** submits the first live code from a freshly-scanned authenticator app against an mfaChallenge of kind "setup" */
  confirmMfaSetup: (code: string) => Promise<void>;
  /** abandons an in-progress MFA challenge and returns to the plain login form */
  cancelMfa: () => void;
  /** dismisses the one-time recovery-codes screen once the person has saved them */
  dismissRecoveryCodes: () => void;
  logout: () => Promise<void>;
  continueAsDemo: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

// Session-scoped (not localStorage) so closing the tab drops back to a
// real sign-in prompt next time, rather than a demo choice persisting
// indefinitely.
const DEMO_FLAG_KEY = "ledger:demo-mode";

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [loading, setLoading] = useState(true);
  const [demo, setDemo] = useState(() => sessionStorage.getItem(DEMO_FLAG_KEY) === "1");
  const [mfaChallenge, setMfaChallenge] = useState<MfaChallenge | null>(null);
  const [recoveryCodes, setRecoveryCodes] = useState<string[] | null>(null);

  const refresh = useCallback(async () => {
    try {
      const me = await auth.me();
      setUser(me);
    } catch {
      setUser(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const settle = useCallback(async () => {
    // Shared tail end of every path that lands on a real session cookie
    // (plain login, mfa/verify, mfa/setup/confirm): clear demo mode and
    // re-hydrate "who am I" from the cookie that was just set.
    sessionStorage.removeItem(DEMO_FLAG_KEY);
    setDemo(false);
    setMfaChallenge(null);
    await refresh();
  }, [refresh]);

  const login = useCallback(async (identifier: string, password: string) => {
    const result = await auth.login(identifier, password);
    if (result.mfa_required && result.mfa_pending_token) {
      setMfaChallenge({ kind: "verify", token: result.mfa_pending_token });
      return;
    }
    if (result.mfa_setup_required && result.mfa_setup_token && result.totp_secret && result.otpauth_uri) {
      setMfaChallenge({
        kind: "setup",
        token: result.mfa_setup_token,
        totpSecret: result.totp_secret,
        otpauthUri: result.otpauth_uri,
        message: result.message,
      });
      return;
    }
    await settle();
  }, [settle]);

  const verifyMfaCode = useCallback(async (code: string) => {
    if (!mfaChallenge || mfaChallenge.kind !== "verify") {
      throw new Error("There's no verification in progress.");
    }
    const result = await auth.mfaVerify(mfaChallenge.token, code);
    // A RECOVERY code (rather than a live TOTP code) doesn't grant a
    // session by itself -- the account's old secret was just retired
    // along with it, so the backend pivots straight to a fresh
    // mfa_setup_required challenge for this device instead. See
    // README's "Recovery (backup) codes" section.
    if (result.mfa_setup_required && result.mfa_setup_token && result.totp_secret && result.otpauth_uri) {
      setMfaChallenge({
        kind: "setup",
        token: result.mfa_setup_token,
        totpSecret: result.totp_secret,
        otpauthUri: result.otpauth_uri,
        message: result.message,
      });
      return;
    }
    await settle();
  }, [mfaChallenge, settle]);

  const confirmMfaSetup = useCallback(async (code: string) => {
    if (!mfaChallenge || mfaChallenge.kind !== "setup") {
      throw new Error("There's no enrollment in progress.");
    }
    const result = await auth.mfaSetupConfirm(mfaChallenge.token, code);
    if (result.recovery_codes?.length) {
      // Shown exactly once -- hold onto them so Login.tsx can render the
      // recovery-codes screen; settle() already re-hydrates `user`
      // underneath it, so dismissing that screen can navigate immediately.
      setRecoveryCodes(result.recovery_codes);
    }
    await settle();
  }, [mfaChallenge, settle]);

  const cancelMfa = useCallback(() => {
    setMfaChallenge(null);
  }, []);

  const dismissRecoveryCodes = useCallback(() => {
    setRecoveryCodes(null);
  }, []);

  const logout = useCallback(async () => {
    await auth.logout();
    setUser(null);
    sessionStorage.removeItem(DEMO_FLAG_KEY);
    setDemo(false);
  }, []);

  const continueAsDemo = useCallback(() => {
    sessionStorage.setItem(DEMO_FLAG_KEY, "1");
    setDemo(true);
  }, []);

  return (
    <AuthContext.Provider
      value={{
        user,
        loading,
        demo,
        mfaChallenge,
        recoveryCodes,
        login,
        verifyMfaCode,
        confirmMfaSetup,
        cancelMfa,
        dismissRecoveryCodes,
        logout,
        continueAsDemo,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}
