// =============================================================================
// Pure helper functions shared across the Admin/Manager panels -- kept in a
// plain .ts file (no JSX) separate from shared.tsx's AlertDots component, so
// Vite's Fast Refresh can tell components and utilities apart.
// =============================================================================
import { ApiError } from "../../lib/api";

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB"];
  let value = bytes / 1024;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i += 1;
  }
  return `${value.toFixed(1)} ${units[i]}`;
}

export function formatWhen(iso: string): string {
  return new Date(iso).toLocaleString();
}

export function errMsg(err: unknown, fallback: string): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error && err.message) return err.message;
  return fallback;
}

// Role choices offered when provisioning/converting an account. A Manager
// (rather than a full admin) is capped to a "staff"/"customer" subset --
// enforced per-caller in UsersPanel/OutsidersPanel, not here.
export const ROLE_OPTIONS = ["staff", "manager", "admin", "customer"];
