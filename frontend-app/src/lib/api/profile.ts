/**
 * lib/api/profile.ts
 * ----------------------
 * self-service: My Profile (backend/api/auth_api.py) -- available to every
 * role. Identity (name/email/username) rotation is Super Admin only on
 * the legacy frontend; mirrored the same way here (see pages/Profile.tsx).
 */

import type { ProfileDetail } from "../types";
import { rawFetch } from "./core";

export const profileApi = {
  get: () => rawFetch<ProfileDetail>("/auth/me"),
  updatePassword: (userId: number, currentPassword: string, newPassword: string) =>
    rawFetch<{ message?: string }>("/auth/update-password", {
      method: "POST",
      body: JSON.stringify({ user_id: userId, current_password: currentPassword, new_password: newPassword }),
    }),
  updateIdentity: (name: string, email: string, username: string, currentPassword: string, phoneNumber?: string, company?: string) =>
    rawFetch<ProfileDetail & { message?: string }>("/auth/me", {
      method: "PATCH",
      body: JSON.stringify({
        name, email, username, current_password: currentPassword,
        phone_number: phoneNumber, company,
      }),
    }),
  regenerateRecoveryCodes: (password: string) =>
    rawFetch<{ recovery_codes: string[] }>("/auth/mfa/recovery-codes/regenerate", {
      method: "POST",
      body: JSON.stringify({ password }),
    }),
};
