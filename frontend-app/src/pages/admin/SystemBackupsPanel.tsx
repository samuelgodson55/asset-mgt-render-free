// =============================================================================
// System Backups -- ported from the legacy frontend's admin.html "System
// Backups" panel (js/components/backups.js). True Super Admin only.
// =============================================================================
import { useEffect, useState } from "react";
import { motion } from "framer-motion";
import { UploadCloud, DatabaseBackup, HardDrive, Cloud, CloudOff, RotateCcw, Trash2, ShieldAlert, Lock, Loader2, CheckCircle2 } from "lucide-react";
import { useAuth } from "../../lib/useAuth";
import { useNavigate } from "react-router-dom";
import { backupApi } from "../../lib/api";
import type { BackupEntry, BackupStatus, RestoreResult } from "../../lib/types";
import { Modal, ModalEyebrow } from "../../components/ui/Modal";
import { ErrorBanner } from "../../components/ui/ErrorBanner";
import { TableShell, TableHead, TablePlaceholderRow } from "../../components/ui/TableShell";
import { errMsg, formatWhen, formatBytes } from "./sharedHelpers";
import { useRequestGuard } from "../../lib/useRequestGuard";

const TRIGGER_LABELS: Record<BackupEntry["triggered_by"], string> = {
  manual: "Manual",
  scheduled: "Scheduled",
  pre_restore_safety: "Pre-restore safety",
};

type PendingRestore = { mode: "local"; filename: string } | { mode: "upload" };

function RestoreModal({
  pending,
  onClose,
  onDone,
}: {
  pending: PendingRestore | null;
  onClose: () => void;
  onDone: (result: RestoreResult) => void;
}) {
  const [confirmText, setConfirmText] = useState("");
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setConfirmText("");
    setUploadFile(null);
    setError(null);
  }, [pending]);

  if (!pending) return null;

  const targetLabel = pending.mode === "local" ? pending.filename : "the file you upload below";
  const canSubmit = confirmText.trim() === "RESTORE" && (pending.mode === "local" || !!uploadFile) && !submitting;

  const submit = async () => {
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      const result = pending.mode === "local" ? await backupApi.restoreLocal(pending.filename) : await backupApi.restoreUpload(uploadFile!);
      onDone(result);
    } catch (err) {
      setError(errMsg(err, "Restore failed."));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal onClose={onClose} size="md" tone="danger">
      <ModalEyebrow icon={<ShieldAlert size={16} />} label="Destructive action" tone="danger" />
      <h2 className="font-display text-lg font-semibold text-text">Restore the database?</h2>
        <p className="text-[13px] text-text-muted mt-2">
          This replaces the <span className="text-text">entire</span> database with <span className="font-mono text-[12px] text-text break-all">{targetLabel}</span>. A safety backup of the current state is taken first, but everyone -- including you -- will be signed out immediately after.
        </p>

        {pending.mode === "upload" && (
          <label className="block mt-4">
            <span className="text-[11px] uppercase tracking-wider text-text-faint">Backup file (.sql.gz)</span>
            <input
              type="file"
              accept=".gz,.sql,.sql.gz"
              onChange={(e) => setUploadFile(e.target.files?.[0] ?? null)}
              className="w-full mt-1.5 text-[12px] text-text-muted file:mr-3 file:rounded-[3px] file:border file:border-border-soft file:bg-ink-soft file:px-2.5 file:py-1.5 file:text-[11.5px] file:text-text"
            />
          </label>
        )}

        <label className="block mt-4">
          <span className="text-[11px] uppercase tracking-wider text-text-faint">
            Type <span className="font-mono text-rust-soft">RESTORE</span> to confirm
          </span>
          <input
            type="text"
            autoFocus
            value={confirmText}
            onChange={(e) => setConfirmText(e.target.value)}
            placeholder="RESTORE"
            className="w-full mt-1.5 bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] font-mono text-text placeholder:text-text-faint focus:border-rust/60 focus:outline-none transition-colors"
          />
        </label>

        {error && <div className="mt-3"><ErrorBanner>{error}</ErrorBanner></div>}

        <div className="flex gap-2 mt-5">
          <button onClick={onClose} className="flex-1 border border-border-soft hover:border-border text-text text-[13px] rounded-[3px] py-2.5 transition-colors">
            Cancel
          </button>
          <button
            onClick={submit}
            disabled={!canSubmit}
            className="flex-1 flex items-center justify-center gap-2 bg-rust hover:bg-rust-soft disabled:opacity-50 disabled:cursor-not-allowed text-white font-medium text-[13px] rounded-[3px] py-2.5 transition-colors"
          >
            {submitting ? <Loader2 size={13} className="animate-spin" /> : <RotateCcw size={13} />}
            {submitting ? "Restoring…" : "Restore"}
          </button>
        </div>
    </Modal>
  );
}

function RestoreCompleteModal({ result, onContinue }: { result: RestoreResult | null; onContinue: () => void }) {
  if (!result) return null;
  const cred = result.credential_reconciliation;
  const outsider = result.outsider_reconciliation;
  const activity = result.asset_activity_reconciliation;
  const resetCount = cred?.super_admins_reset ?? 0;
  const reinserted = cred?.users_reinserted ?? 0;
  const outsidersReinserted = outsider?.outsiders_reinserted ?? 0;
  const checkoutsPreserved = (activity?.checkouts_reconciled ?? 0) + (activity?.checkouts_reinserted ?? 0);
  const quotationsPreserved = (activity?.quotations_reconciled ?? 0) + (activity?.quotations_reinserted ?? 0);
  const skipped = (activity?.checkouts_skipped ?? 0) + (activity?.quotations_skipped ?? 0);

  return (
    <Modal size="md" tone="success" dismissOnBackdropClick={false}>
      <ModalEyebrow icon={<CheckCircle2 size={16} />} label="Restore complete" tone="success" />
      <h2 className="font-display text-lg font-semibold text-text">The database has been replaced</h2>
        <div className="mt-3 flex flex-col gap-1.5 text-[12.5px] text-text-muted">
          {resetCount > 0 && <p>Your password still works, but you'll need to set up two-factor authentication again after logging back in.</p>}
          {reinserted > 0 && <p>{reinserted} account(s) created since the backup was taken {reinserted === 1 ? "was" : "were"} restored along with their current password.</p>}
          {outsidersReinserted > 0 && <p>{outsidersReinserted} ad-hoc profile(s) added since the backup was taken {outsidersReinserted === 1 ? "was" : "were"} also restored.</p>}
          {(checkoutsPreserved > 0 || quotationsPreserved > 0) && (
            <p>Current checkout/quotation activity for preserved accounts was kept up to date ({checkoutsPreserved} checkout(s), {quotationsPreserved} quotation(s)).</p>
          )}
          {skipped > 0 && (
            <p className="text-rust-soft">{skipped} checkout/quotation record(s) could not be safely carried forward and were skipped -- see the Audit Trail for details.</p>
          )}
        </div>
        <p className="text-[12.5px] text-text-muted mt-3">For security, everyone is being signed out now, including you.</p>
        <button
          onClick={onContinue}
          className="w-full mt-5 flex items-center justify-center gap-2 bg-brass hover:bg-brass-soft text-ink font-medium text-[13px] rounded-[3px] py-2.5 transition-colors"
        >
          <Lock size={13} /> Sign out and continue
        </button>
    </Modal>
  );
}

export function SystemBackupsPanel() {
  const [status, setStatus] = useState<BackupStatus | null>(null);
  const [backups, setBackups] = useState<BackupEntry[]>([]);
  const [loadingList, setLoadingList] = useState(true);
  const [backingUp, setBackingUp] = useState(false);
  const [pendingRestore, setPendingRestore] = useState<PendingRestore | null>(null);
  const [restoreResult, setRestoreResult] = useState<RestoreResult | null>(null);
  const { logout } = useAuth();
  const navigate = useNavigate();
  const beginRequest = useRequestGuard();

  const refresh = async () => {
    const isCurrent = beginRequest();
    setLoadingList(true);
    try {
      const [s, list] = await Promise.all([backupApi.status(), backupApi.list()]);
      if (!isCurrent()) return;
      setStatus(s);
      setBackups(list);
    } catch (err) {
      if (isCurrent()) console.error("Failed to load backup status/list:", err);
    } finally {
      if (isCurrent()) setLoadingList(false);
    }
  };

  useEffect(() => {
    refresh();
  }, []);

  const createNow = async () => {
    setBackingUp(true);
    try {
      await backupApi.create();
      await refresh();
    } catch (err) {
      alert(errMsg(err, "Backup failed."));
    } finally {
      setBackingUp(false);
    }
  };

  const download = (filename: string) => {
    const a = document.createElement("a");
    a.href = backupApi.downloadUrl(filename);
    a.download = filename;
    a.click();
  };

  const remove = async (filename: string) => {
    if (!confirm(`Delete local backup "${filename}"? This does not affect any copy already uploaded to Google Drive.`)) return;
    try {
      await backupApi.remove(filename);
      await refresh();
    } catch (err) {
      alert(errMsg(err, "Delete failed."));
    }
  };

  const finishRestore = async () => {
    setRestoreResult(null);
    setPendingRestore(null);
    await logout();
    navigate("/login");
  };

  return (
    <div className="flex flex-col gap-4">
      <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.35 }} className="border border-border-soft bg-surface rounded-[3px] p-5">
        <div className="flex items-center justify-between mb-4 flex-wrap gap-3">
          <div className="flex items-center gap-2.5">
            <div className="w-9 h-9 rounded-full bg-brass/10 flex items-center justify-center shrink-0">
              <DatabaseBackup size={16} className="text-brass-soft" />
            </div>
            <div>
              <h2 className="font-display text-[15px] font-medium text-text">System backups</h2>
              <p className="text-[11.5px] text-text-faint">Restricted to the root Super Admin account</p>
            </div>
          </div>
          <div className="flex items-center gap-2 flex-wrap">
            <button
              onClick={() => setPendingRestore({ mode: "upload" })}
              className="flex items-center gap-1.5 border border-border-soft hover:border-brass/40 text-text text-[12px] rounded-[3px] px-3 py-1.5 transition-colors"
            >
              <UploadCloud size={12} /> Restore from upload
            </button>
            <button
              onClick={createNow}
              disabled={backingUp}
              className="flex items-center gap-1.5 bg-brass hover:bg-brass-soft disabled:opacity-60 text-ink font-medium text-[12px] rounded-[3px] px-3 py-1.5 transition-colors"
            >
              {backingUp ? <Loader2 size={12} className="animate-spin" /> : <HardDrive size={12} />}
              {backingUp ? "Backing up…" : "Backup now"}
            </button>
          </div>
        </div>

        {status ? (
          <>
            <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
              <div>
                <p className="text-[11px] uppercase tracking-wide text-text-faint">Daily schedule</p>
                <p className="text-[13px] font-semibold text-text mt-0.5">
                  {status.auto_backup_enabled ? `${status.backup_hours_display.join(", ")} ${status.display_timezone_label}` : "Disabled"}
                </p>
              </div>
              <div>
                <p className="text-[11px] uppercase tracking-wide text-text-faint">Google Drive sync</p>
                <p className={`text-[13px] font-semibold mt-0.5 flex items-center gap-1.5 ${status.gdrive_enabled ? "text-moss-soft" : "text-text-faint"}`}>
                  {status.gdrive_enabled ? <Cloud size={12} /> : <CloudOff size={12} />}
                  {status.gdrive_enabled ? "Enabled" : "Not configured"}
                </p>
              </div>
              <div>
                <p className="text-[11px] uppercase tracking-wide text-text-faint">Local backups kept</p>
                <p className="text-[13px] font-semibold text-text mt-0.5">{status.backup_count} / {status.retention_count}</p>
              </div>
              <div>
                <p className="text-[11px] uppercase tracking-wide text-text-faint">Last backup</p>
                <p className="text-[13px] font-semibold text-text mt-0.5">{status.latest_backup ? formatWhen(status.latest_backup.created_at) : "None yet"}</p>
              </div>
            </div>
            {!status.gdrive_enabled && (
              <p className="mt-3 rounded-md border border-brass/30 bg-brass/10 px-3 py-2 text-[12px] text-brass-soft">
                Google Drive sync is off -- local backups do not survive a redeploy or spin-down on a free hosting plan. Set the Drive credentials to make backups durable.
              </p>
            )}
          </>
        ) : (
          <p className="text-[12px] text-text-faint">Loading status…</p>
        )}
      </motion.div>

      <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.35, delay: 0.06 }}>
        <TableShell>
          <table className="w-full text-left text-[12.5px]">
            <TableHead>
              <th className="px-5 py-3 font-medium">File</th>
              <th className="hidden sm:table-cell px-5 py-3 font-medium">Created</th>
              <th className="hidden sm:table-cell px-5 py-3 font-medium">Size / Trigger</th>
              <th className="hidden sm:table-cell px-5 py-3 font-medium">Cloud sync</th>
              <th className="hidden sm:table-cell px-5 py-3 font-medium text-right">Actions</th>
            </TableHead>
            <tbody className="divide-y divide-border-soft">
              {loadingList && <TablePlaceholderRow columns={5}>Loading backups…</TablePlaceholderRow>}
              {!loadingList && backups.length === 0 && (
                <TablePlaceholderRow columns={5}>No backups yet -- click "Backup now" to create one.</TablePlaceholderRow>
              )}
              {backups.map((entry) => {
                const badge = entry.gdrive_error ? (
                  <span className="inline-flex items-center gap-1 rounded-full border border-rust/40 bg-rust/10 px-2 py-0.5 text-[11px] font-medium text-rust-soft" title={entry.gdrive_error}>Drive upload failed</span>
                ) : entry.gdrive_uploaded ? (
                  <span className="inline-flex items-center gap-1 rounded-full border border-moss/40 bg-moss/10 px-2 py-0.5 text-[11px] font-medium text-moss-soft">Synced to Drive</span>
                ) : (
                  <span className="inline-flex items-center gap-1 rounded-full border border-border bg-surface-raised px-2 py-0.5 text-[11px] font-medium text-text-faint">Local only</span>
                );
                const actions = (
                  <>
                    <button onClick={() => download(entry.filename)} title="Download" className="rounded-md border border-border-soft bg-surface-raised px-2.5 py-1 text-[12px] font-medium text-text-muted transition hover:border-sky/50 hover:text-sky">Download</button>
                    <button onClick={() => setPendingRestore({ mode: "local", filename: entry.filename })} title="Restore the database from this backup" className="rounded-md border border-brass/40 bg-brass/10 px-2.5 py-1 text-[12px] font-medium text-brass-soft transition hover:bg-brass/20">Restore</button>
                    <button onClick={() => remove(entry.filename)} title="Delete this local backup file" className="rounded-md border border-border-soft bg-surface-raised px-2.5 py-1 text-[12px] font-medium text-text-muted transition hover:border-rust/50 hover:text-rust-soft">
                      <Trash2 size={11} className="inline -mt-0.5" />
                    </button>
                  </>
                );
                return (
                  <tr key={entry.filename}>
                    <td className="px-5 py-3">
                      <p className="break-all font-mono text-[12px] text-text">{entry.filename}</p>
                      <p className="mt-1 text-[11px] text-text-faint sm:hidden">{formatWhen(entry.created_at)} · {formatBytes(entry.size_bytes)} · {TRIGGER_LABELS[entry.triggered_by]}</p>
                      <div className="mt-2 sm:hidden">{badge}</div>
                      <div className="mt-3 flex flex-wrap gap-2 sm:hidden">{actions}</div>
                    </td>
                    <td className="hidden sm:table-cell px-5 py-3 text-text-muted">{formatWhen(entry.created_at)}</td>
                    <td className="hidden sm:table-cell px-5 py-3 text-text-muted">{formatBytes(entry.size_bytes)} · {TRIGGER_LABELS[entry.triggered_by]}</td>
                    <td className="hidden sm:table-cell px-5 py-3">{badge}</td>
                    <td className="hidden sm:table-cell px-5 py-3 text-right"><div className="flex items-center justify-end gap-2">{actions}</div></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </TableShell>
      </motion.div>

      <RestoreModal
        pending={pendingRestore}
        onClose={() => setPendingRestore(null)}
        onDone={(result) => {
          setPendingRestore(null);
          setRestoreResult(result);
        }}
      />
      <RestoreCompleteModal result={restoreResult} onContinue={finishRestore} />
    </div>
  );
}
