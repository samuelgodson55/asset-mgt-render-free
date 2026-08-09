import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { auth, quotationsApi, type AuthUser } from "./api";
import { canSeeStock as computeCanSeeStock } from "./roles";
import { AuthContext, type MfaChallenge } from "./auth-context";

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
  // Safe-by-default (false) until GET /config/public resolves -- see
  // canSeeStock's docstring above.
  const [catalogShowStock, setCatalogShowStock] = useState(false);

  useEffect(() => {
    // Public, unauthenticated-safe endpoint (also powers the Login page's
    // site_name) -- fetched once per app load, independent of sign-in
    // state, so it's ready by the time `user`/`demo` settle.
    quotationsApi.publicConfig().then((config) => {
      setCatalogShowStock(!!config.show_stock_to_staff_customer);
    });
  }, []);

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

  const canSeeStock = useMemo(
    () => computeCanSeeStock(user?.role, demo, catalogShowStock),
    [user?.role, demo, catalogShowStock]
  );

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
        canSeeStock,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}
