/**
 * lib/api/auth.ts
 * ------------------
 * POST /auth/login sets an httpOnly session cookie itself (see
 * backend/api/auth_api.py's _set_session_cookie); there's no token for the
 * frontend to store. GET /auth/me re-hydrates "who am I" on load / refresh.
 */

import { rawFetch } from "./core";


export interface AuthUser {
  sub?: string;
  id?: number;
  name: string;
  email: string;
  username?: string | null;
  role: string;
  department?: string | null;
  department_role?: string | null;
}

export interface LoginResult {
  message?: string;
  mfa_required?: boolean;
  mfa_setup_required?: boolean;
  mfa_pending_token?: string;
  mfa_setup_token?: string;
  // Present only on an mfa_setup_required response (fresh enrollment, or
  // re-enrollment after a recovery code was used) -- shown exactly once,
  // never retrievable again. See README's "Two-factor authentication" section.
  totp_secret?: string;
  otpauth_uri?: string;
  // Present only on the response that finally confirms first-time 2FA
  // enrollment (POST /auth/mfa/setup/confirm) -- ten single-use backup
  // codes, also shown exactly once.
  recovery_codes?: string[];
}

export const auth = {
  // Backend's LoginRequest takes `identifier` (matched against either the
  // email or username column), not `email` -- see README.md's /auth/login
  // entry and schemas/auth_schema.py.
  login: (identifier: string, password: string) =>
    rawFetch<LoginResult>("/auth/login", { method: "POST", body: JSON.stringify({ identifier, password }) }),
  mfaVerify: (mfa_pending_token: string, code: string) =>
    rawFetch<LoginResult>("/auth/mfa/verify", { method: "POST", body: JSON.stringify({ mfa_pending_token, code }) }),
  // Completes first-time 2FA enrollment (or re-enrollment after a recovery
  // code retired the old secret): mfa_setup_token from the mfa_setup_required
  // response above, plus a live code from the authenticator app that was
  // just set up. This is the call that actually grants the session cookie.
  mfaSetupConfirm: (mfa_setup_token: string, code: string) =>
    rawFetch<LoginResult>("/auth/mfa/setup/confirm", { method: "POST", body: JSON.stringify({ mfa_setup_token, code }) }),
  logout: () => rawFetch<{ message: string }>("/auth/logout", { method: "POST" }),
  me: () => rawFetch<AuthUser>("/auth/me"),
  // POST /auth/forgot-password -- backend always responds with the same
  // generic message whether or not `identifier` matches an account (see
  // schemas/auth_schema.py's ForgotPasswordRequest docstring), so there's
  // nothing for the UI to branch on beyond a genuine network/server error.
  forgotPassword: (identifier: string) =>
    rawFetch<{ message?: string }>("/auth/forgot-password", { method: "POST", body: JSON.stringify({ identifier }) }),
  // POST /auth/reset-password -- completes the flow using the plaintext
  // `token` off the emailed link's ?reset_token= query param. Doesn't grant
  // a session; the person still signs in normally afterward with the new
  // password.
  resetPassword: (token: string, newPassword: string) =>
    rawFetch<{ message?: string }>("/auth/reset-password", { method: "POST", body: JSON.stringify({ token, new_password: newPassword }) }),
};
