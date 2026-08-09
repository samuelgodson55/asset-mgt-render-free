import { createContext } from "react";
import type { AuthUser } from "./api";

// Mirrors backend/services/auth_service.py's two 2FA challenge shapes --
// see README's "Two-factor authentication (2FA)" section. Only super_admin
// accounts ever produce one of these; every other role's login() resolves
// straight to a session.
export type MfaChallenge =
  | { kind: "setup"; token: string; totpSecret: string; otpauthUri: string; message?: string }
  | { kind: "verify"; token: string };

export interface AuthContextValue {
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
  /**
   * Whether the CURRENT session is entitled to see real-time stock numbers
   * (available_quantity/status) anywhere in the app -- true for every
   * Manager/Admin/Super Admin and demo session unconditionally, and for a
   * Staff/Customer session only when the operator has turned
   * CATALOG_SHOW_STOCK_TO_STAFF_CUSTOMER on. See lib/roles.ts's
   * canSeeStock() for the backend rule this mirrors. Defaults to `false`
   * (the safe state) until GET /config/public has actually resolved, so a
   * Staff/Customer session never briefly renders stock it isn't entitled
   * to while that request is in flight.
   */
  canSeeStock: boolean;
}

export const AuthContext = createContext<AuthContextValue | null>(null);
