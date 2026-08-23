/**
 * lib/api/backup.ts
 * ---------------------
 * admin: System Backups -- every /backup/* route is gated on the backend
 * by deps.require_true_super_admin (root Super Admin only; a regular
 * `admin` account, though otherwise equivalent, is deliberately excluded
 * here -- see backend/deps.py). Callers (pages/Admin.tsx) additionally
 * hide the panel client-side, same belt-and-suspenders spirit as the
 * legacy frontend's isTrueSuperAdmin() check in js/components/backups.js.
 */

import { mockBackupStatus, mockBackups } from "../mock";
import type { BackupEntry, BackupStatus, RestoreResult } from "../types";
import { API_BASE, rawFetch, rawFetchMultipart, tryLoad } from "./core";

export const backupApi = {
  status: () => tryLoad(() => rawFetch<BackupStatus>("/backup/status"), mockBackupStatus),
  list: () => tryLoad(() => rawFetch<BackupEntry[]>("/backup/list"), mockBackups),
  create: () => rawFetch<{ message?: string }>("/backup/create", { method: "POST" }),
  remove: (filename: string) => rawFetch<void>(`/backup/${encodeURIComponent(filename)}`, { method: "DELETE" }),
  restoreLocal: (filename: string) => rawFetch<RestoreResult>(`/backup/restore/${encodeURIComponent(filename)}`, { method: "POST" }),
  restoreUpload: (file: File) => {
    const formData = new FormData();
    formData.append("file", file);
    return rawFetchMultipart<RestoreResult>("/backup/restore-upload", formData);
  },
  // Not a JSON round trip -- the browser needs a plain navigable/`<a>`-able
  // URL (the httpOnly session cookie rides along automatically since this
  // stays same-origin), same approach as the legacy frontend's downloadBackup().
  downloadUrl: (filename: string) => `${API_BASE}/backup/download/${encodeURIComponent(filename)}`,
};
