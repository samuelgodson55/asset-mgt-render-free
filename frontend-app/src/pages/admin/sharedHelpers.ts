// =============================================================================
// Pure helper functions shared across the Admin/Manager panels -- kept in a
// plain .ts file (no JSX) separate from shared.tsx's AlertDots component, so
// Vite's Fast Refresh can tell components and utilities apart.
// =============================================================================
import { formatFriendlyErrorLine, friendlyError } from "../../lib/errors";

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

// Every Admin/Manager panel renders its errors as a single-line status
// message (there wasn't room in this file's scope to redesign every panel
// around the full <ErrorState> card -- see components/ui/ErrorBanner.tsx --
// used elsewhere in the app). This keeps that compact rendering but routes
// the actual message through the same friendlyError() classification
// everything else uses, so a raw "HTTP 500" never surfaces here either, and
// the correlation ID a failed request returns is appended for support
// instead of being silently dropped.
export function errMsg(err: unknown, fallback: string): string {
  return formatFriendlyErrorLine(friendlyError(err, fallback));
}

// Role choices offered when provisioning/converting an account. A Manager
// (rather than a full admin) is capped to a "staff"/"customer" subset --
// enforced per-caller in UsersPanel/OutsidersPanel, not here.
export const ROLE_OPTIONS = ["staff", "manager", "admin", "customer"];
