import { useEffect, useMemo, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
  UploadCloud,
  Download,
  FileSpreadsheet,
  DatabaseBackup,
  HardDrive,
  Cloud,
  CloudOff,
  RotateCcw,
  Trash2,
  ShieldAlert,
  X,
  Plus,
  Mail,
  Loader2,
  CheckCircle2,
  TriangleAlert,
  Lock,
  Users as UsersIcon,
  Contact,
  ScrollText,
  Search,
  KeyRound,
} from "lucide-react";
import { useAuth } from "../lib/auth";
import { useNavigate } from "react-router-dom";
import { backupApi, digestApi, importApi, usersApi, outsidersApi, auditApi, ApiError } from "../lib/api";
import type { BackupEntry, BackupStatus, ImportResult, RestoreResult, UserRow, OutsiderRow, AuditLogEntry } from "../lib/types";
import { isFullAdmin, isTrueSuperAdmin, isPrivileged } from "../lib/roles";
import { CustodyDrawer } from "../components/CustodyDrawer";

function formatBytes(bytes: number): string {
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

function formatWhen(iso: string): string {
  return new Date(iso).toLocaleString();
}

const TRIGGER_LABELS: Record<BackupEntry["triggered_by"], string> = {
  manual: "Manual",
  scheduled: "Scheduled",
  pre_restore_safety: "Pre-restore safety",
};

function errMsg(err: unknown, fallback: string): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error && err.message) return err.message;
  return fallback;
}

// =============================================================================
// Inventory Import -- ported from the legacy frontend's
// js/components/assets.js (downloadCsvImportTemplate / submitCsvImportForm).
// Available to Super Admin AND a plain Admin account (require_super_admin
// on POST /assets/import -- the broader "full admin" gate).
// =============================================================================

const CSV_IMPORT_TEMPLATE_ROWS = [
  ["name", "total_quantity", "category", "price"],
  ["Dell Latitude 5440", "10", "Engineering", "899.00"],
  ["Logitech MX Master 3S", "25", "Engineering", "99.99"],
  ["Herman Miller Aeron Chair", "8", "", "1395.00"],
];

function downloadCsvTemplate() {
  const csvText = CSV_IMPORT_TEMPLATE_ROWS.map((row) =>
    row.map((cell) => (/[",\n]/.test(cell) ? `"${cell.replace(/"/g, '""')}"` : cell)).join(",")
  ).join("\r\n");
  const blob = new Blob([csvText], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "asset_import_template.csv";
  a.click();
  URL.revokeObjectURL(url);
}

function InventoryImportPanel() {
  const [file, setFile] = useState<File | null>(null);
  const [fileText, setFileText] = useState<string>("");
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState<ImportResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const onFile = async (f: File | null) => {
    setFile(f);
    setResult(null);
    setError(null);
    if (!f) {
      setFileText("");
      return;
    }
    try {
      setFileText(await f.text());
    } catch {
      setFileText("");
    }
  };

  const submit = async () => {
    if (!file) return;
    setSubmitting(true);
    setError(null);
    try {
      const res = await importApi.importCsv(file);
      setResult(res);
      setFile(null);
    } catch (err) {
      setError(errMsg(err, "Import failed."));
    } finally {
      setSubmitting(false);
    }
  };

  const downloadFailedRows = () => {
    if (!result?.errors.length || !fileText) return;
    const lines = fileText.split(/\r\n|\n|\r/);
    const header = lines[0] || "";
    const failed = result.errors.map((e) => lines[e.row - 1] || "").filter(Boolean);
    const csvText = [header, ...failed].join("\r\n");
    const blob = new Blob([csvText], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `failed_import_rows_${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const total = (result?.imported_count ?? 0) + (result?.errors.length ?? 0);

  return (
    <div className="grid grid-cols-1 lg:grid-cols-5 gap-4">
      <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.35 }} className="lg:col-span-3 border border-border-soft bg-surface rounded-[3px] p-5">
        <div className="flex items-start gap-3 mb-5">
          <div className="w-9 h-9 rounded-full bg-sky/10 flex items-center justify-center shrink-0">
            <FileSpreadsheet size={16} className="text-sky" />
          </div>
          <div>
            <h2 className="font-display text-[15px] font-medium text-text">Bulk import asset inventory</h2>
            <p className="text-[12.5px] text-text-muted mt-0.5">
              Upload a CSV of <span className="font-mono text-[11.5px]">name, total_quantity, category, price</span> rows. Matching pool names <span className="text-text">add</span> to the existing quantity rather than replacing it.
            </p>
          </div>
        </div>

        <button
          type="button"
          onClick={downloadCsvTemplate}
          className="flex items-center gap-2 text-[12px] text-brass-soft border border-border-soft hover:border-brass/40 rounded-[3px] px-3 py-1.5 mb-5 transition-colors"
        >
          <Download size={12} /> Download sample template
        </button>

        <label
          className={`flex flex-col items-center justify-center gap-2 border-2 border-dashed rounded-[4px] py-10 px-4 cursor-pointer transition-colors ${
            file ? "border-brass/50 bg-brass/5" : "border-border-soft hover:border-border"
          }`}
        >
          <UploadCloud size={22} className={file ? "text-brass-soft" : "text-text-faint"} />
          <span className="text-[12.5px] text-text-muted text-center">
            {file ? <span className="text-text font-medium">{file.name}</span> : "Click to choose a .csv file, or drag it here"}
          </span>
          <input type="file" accept=".csv,text/csv" className="hidden" onChange={(e) => onFile(e.target.files?.[0] ?? null)} />
        </label>

        {error && (
          <div className="flex items-start gap-2 bg-rust/10 border border-rust/30 text-rust-soft text-[12px] rounded-[3px] px-3 py-2.5 mt-4">
            <TriangleAlert size={13} className="mt-0.5 shrink-0" />
            <span>{error}</span>
          </div>
        )}

        <button
          type="button"
          disabled={!file || submitting}
          onClick={submit}
          className="mt-4 w-full flex items-center justify-center gap-2 bg-brass hover:bg-brass-soft disabled:opacity-50 disabled:cursor-not-allowed text-ink font-medium text-[13px] rounded-[3px] py-2.5 transition-colors"
        >
          {submitting ? <Loader2 size={14} className="animate-spin" /> : <UploadCloud size={14} />}
          {submitting ? "Importing…" : "Import CSV"}
        </button>

        <AnimatePresence>
          {result && (
            <motion.div initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: "auto" }} exit={{ opacity: 0, height: 0 }} className="overflow-hidden">
              <div
                className={`mt-4 rounded-[3px] px-4 py-3 text-[13px] font-medium flex items-center gap-2 ${
                  result.errors.length ? "bg-brass/10 border border-brass/30 text-brass-soft" : "bg-moss/10 border border-moss/30 text-moss-soft"
                }`}
              >
                {result.errors.length ? <TriangleAlert size={14} /> : <CheckCircle2 size={14} />}
                Import complete: {result.imported_count} of {total} records saved ({result.errors.length} failed)
              </div>

              {result.errors.length > 0 && (
                <>
                  <button
                    type="button"
                    onClick={downloadFailedRows}
                    disabled={!fileText}
                    className="mt-3 flex items-center gap-2 text-[12px] text-ink bg-brass hover:bg-brass-soft disabled:opacity-50 rounded-[3px] px-3 py-1.5 transition-colors"
                  >
                    <Download size={12} /> Download failed rows (.csv)
                  </button>
                  <div className="mt-3 border border-border-soft rounded-[3px] overflow-hidden">
                    <table className="w-full text-left text-[12px]">
                      <thead className="bg-surface-raised">
                        <tr className="text-text-faint">
                          <th className="px-3 py-2 font-medium">Row</th>
                          <th className="px-3 py-2 font-medium">Value</th>
                          <th className="px-3 py-2 font-medium">Reason</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-border-soft">
                        {result.errors.map((e, i) => (
                          <tr key={i}>
                            <td className="px-3 py-2 align-top text-text-muted">{e.row}</td>
                            <td className="px-3 py-2 align-top text-text-muted">{e.name || "—"}</td>
                            <td className="px-3 py-2 align-top text-rust-soft">{e.reason}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </>
              )}
            </motion.div>
          )}
        </AnimatePresence>
      </motion.div>

      <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.35, delay: 0.08 }} className="lg:col-span-2 border border-border-soft bg-surface rounded-[3px] p-5">
        <h2 className="font-display text-[15px] font-medium text-text mb-3">Expected columns</h2>
        <div className="flex flex-col gap-3 text-[12.5px]">
          {[
            ["name", "Pool name -- matches an existing pool by exact name, or creates a new one."],
            ["total_quantity", "Whole number. Added to the existing total if the pool already exists."],
            ["category", "Optional. Leave blank to keep uncategorized."],
            ["price", "Optional unit price, e.g. 899.00."],
          ].map(([col, desc]) => (
            <div key={col} className="border-l-2 border-border-soft pl-3">
              <p className="font-mono text-[11.5px] text-brass-soft">{col}</p>
              <p className="text-text-muted mt-0.5">{desc}</p>
            </div>
          ))}
        </div>
      </motion.div>
    </div>
  );
}

// =============================================================================
// System Backups -- ported from the legacy frontend's admin.html "System
// Backups" panel (js/components/backups.js). True Super Admin only.
// =============================================================================

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
    <AnimatePresence>
      <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onClick={onClose} className="fixed inset-0 bg-ink/70 backdrop-blur-sm z-40" />
      <motion.div
        initial={{ opacity: 0, y: 16, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        exit={{ opacity: 0, y: 8, scale: 0.98 }}
        transition={{ duration: 0.25, ease: [0.16, 1, 0.3, 1] }}
        className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-50 w-full max-w-md bg-surface border border-rust/30 rounded-[4px] p-6"
      >
        <div className="flex items-center gap-2 text-rust-soft mb-2">
          <ShieldAlert size={16} />
          <span className="text-[11px] uppercase tracking-wider">Destructive action</span>
        </div>
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

        {error && (
          <div className="flex items-start gap-2 bg-rust/10 border border-rust/30 text-rust-soft text-[12px] rounded-[3px] px-3 py-2.5 mt-3">
            <TriangleAlert size={13} className="mt-0.5 shrink-0" />
            <span>{error}</span>
          </div>
        )}

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
      </motion.div>
    </AnimatePresence>
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
    <AnimatePresence>
      <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="fixed inset-0 bg-ink/80 backdrop-blur-sm z-40" />
      <motion.div
        initial={{ opacity: 0, y: 16, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        transition={{ duration: 0.25, ease: [0.16, 1, 0.3, 1] }}
        className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-50 w-full max-w-md bg-surface border border-moss/30 rounded-[4px] p-6"
      >
        <div className="flex items-center gap-2 text-moss-soft mb-2">
          <CheckCircle2 size={16} />
          <span className="text-[11px] uppercase tracking-wider">Restore complete</span>
        </div>
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
      </motion.div>
    </AnimatePresence>
  );
}

function DigestRecipients() {
  const [emails, setEmails] = useState<string[]>([]);
  const [input, setInput] = useState("");
  const [message, setMessage] = useState<{ text: string; tone: "ok" | "err" } | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    digestApi.list().then((e) => {
      setEmails(e);
      setLoading(false);
    });
  }, []);

  const save = async (next: string[], successMessage: string) => {
    try {
      const res = await digestApi.set(next);
      setEmails(res.emails ?? next);
      setMessage({ text: successMessage, tone: "ok" });
    } catch (err) {
      setMessage({ text: errMsg(err, "Couldn't save."), tone: "err" });
    }
  };

  const add = async (e: React.FormEvent) => {
    e.preventDefault();
    const email = input.trim().toLowerCase();
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) {
      setMessage({ text: "Enter a valid email address.", tone: "err" });
      return;
    }
    if (emails.includes(email)) {
      setMessage({ text: "That address is already on the list.", tone: "err" });
      return;
    }
    await save([...emails, email], `${email} will now receive the daily digest.`);
    setInput("");
  };

  const remove = (email: string) => save(emails.filter((e) => e !== email), `${email} removed from the daily digest.`);

  return (
    <div className="border border-border-soft bg-surface rounded-[3px] p-5">
      <div className="flex items-start gap-3 mb-4">
        <div className="w-9 h-9 rounded-full bg-sky/10 flex items-center justify-center shrink-0">
          <Mail size={16} className="text-sky" />
        </div>
        <div>
          <h2 className="font-display text-[15px] font-medium text-text">Daily digest recipients</h2>
          <p className="text-[12.5px] text-text-muted mt-0.5">The once-a-day overdue/due-soon summary email goes to these addresses only -- being an Admin or Manager no longer implies receiving it.</p>
        </div>
      </div>

      {loading ? (
        <p className="text-[12px] text-text-faint">Loading…</p>
      ) : (
        <div className="flex flex-wrap gap-2 mb-4">
          {emails.length === 0 && <span className="text-[12px] text-text-faint">No recipients configured -- the daily digest currently has nowhere to send.</span>}
          {emails.map((email) => (
            <span key={email} className="flex items-center gap-2 rounded-full border border-border-soft bg-surface-raised py-1 pl-3 pr-1.5 text-[12px] text-text">
              {email}
              <button onClick={() => remove(email)} title="Remove" className="rounded-full p-0.5 text-text-faint hover:bg-rust/10 hover:text-rust-soft transition-colors">
                <X size={11} />
              </button>
            </span>
          ))}
        </div>
      )}

      <form onSubmit={add} className="flex gap-2">
        <input
          type="email"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="name@organization.com"
          className="flex-1 bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2 text-[12.5px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none transition-colors"
        />
        <button type="submit" className="flex items-center gap-1.5 bg-brass hover:bg-brass-soft text-ink font-medium text-[12.5px] rounded-[3px] px-3 transition-colors">
          <Plus size={13} /> Add
        </button>
      </form>
      {message && <p className={`text-[12px] mt-2 font-medium ${message.tone === "ok" ? "text-moss-soft" : "text-rust-soft"}`}>{message.text}</p>}
    </div>
  );
}

function SystemBackupsPanel() {
  const [status, setStatus] = useState<BackupStatus | null>(null);
  const [backups, setBackups] = useState<BackupEntry[]>([]);
  const [loadingList, setLoadingList] = useState(true);
  const [backingUp, setBackingUp] = useState(false);
  const [pendingRestore, setPendingRestore] = useState<PendingRestore | null>(null);
  const [restoreResult, setRestoreResult] = useState<RestoreResult | null>(null);
  const { logout } = useAuth();
  const navigate = useNavigate();

  const refresh = async () => {
    setLoadingList(true);
    const [s, list] = await Promise.all([backupApi.status(), backupApi.list()]);
    setStatus(s);
    setBackups(list);
    setLoadingList(false);
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
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2.5">
            <div className="w-9 h-9 rounded-full bg-brass/10 flex items-center justify-center shrink-0">
              <DatabaseBackup size={16} className="text-brass-soft" />
            </div>
            <div>
              <h2 className="font-display text-[15px] font-medium text-text">System backups</h2>
              <p className="text-[11.5px] text-text-faint">Restricted to the root Super Admin account</p>
            </div>
          </div>
          <div className="flex items-center gap-2">
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

      <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.35, delay: 0.06 }} className="border border-border-soft bg-surface rounded-[3px] overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-left text-[12.5px]">
            <thead className="bg-surface-raised text-text-faint text-[11px] uppercase tracking-wide">
              <tr>
                <th className="px-5 py-3 font-medium">File</th>
                <th className="hidden sm:table-cell px-5 py-3 font-medium">Created</th>
                <th className="hidden sm:table-cell px-5 py-3 font-medium">Size / Trigger</th>
                <th className="hidden sm:table-cell px-5 py-3 font-medium">Cloud sync</th>
                <th className="hidden sm:table-cell px-5 py-3 font-medium text-right">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border-soft">
              {loadingList && (
                <tr><td colSpan={5} className="px-5 py-6 text-center text-text-faint">Loading backups…</td></tr>
              )}
              {!loadingList && backups.length === 0 && (
                <tr><td colSpan={5} className="px-5 py-6 text-center text-text-faint">No backups yet -- click "Backup now" to create one.</td></tr>
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
        </div>
      </motion.div>

      <DigestRecipients />

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

// =============================================================================
// User Directory -- ported from js/components/users.js. List/search/custody
// is require_privileged_role; reset-password/delete/restore are Super Admin
// only (require_super_admin), so those affordances are hidden for a plain
// Admin/Manager even though they can see the directory itself.
// =============================================================================

const ROLE_OPTIONS = ["staff", "manager", "admin", "customer"];
const PAGE_SIZE = 20;

function AlertDots({ alerts }: { alerts: UserRow["alerts"] | OutsiderRow["alerts"] }) {
  if (!alerts.overdue && !alerts.due_soon && !alerts.pending_extension) return null;
  return (
    <span className="inline-flex items-center gap-1 ml-1.5">
      {alerts.overdue && <span title="Has an overdue item" className="w-1.5 h-1.5 rounded-full bg-rust" />}
      {!alerts.overdue && alerts.due_soon && <span title="Has an item due soon" className="w-1.5 h-1.5 rounded-full bg-brass" />}
      {alerts.pending_extension && <span title="Has a pending extension request" className="w-1.5 h-1.5 rounded-full bg-sky" />}
    </span>
  );
}

function CreateUserModal({ open, onClose, onCreated }: { open: boolean; onClose: () => void; onCreated: () => void }) {
  const [form, setForm] = useState({ name: "", email: "", phone_number: "", role: "staff", password: "", department: "", department_role: "" });
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (open) {
      setForm({ name: "", email: "", phone_number: "", role: "staff", password: "", department: "", department_role: "" });
      setError(null);
    }
  }, [open]);

  if (!open) return null;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await usersApi.create(form);
      onCreated();
    } catch (err) {
      setError(errMsg(err, "Couldn't create the account."));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <AnimatePresence>
      <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onClick={onClose} className="fixed inset-0 bg-ink/70 backdrop-blur-sm z-40" />
      <motion.div
        initial={{ opacity: 0, y: 16, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        exit={{ opacity: 0, y: 8, scale: 0.98 }}
        className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-50 w-full max-w-md bg-surface border border-border-soft rounded-[4px] p-6 max-h-[85vh] overflow-y-auto"
      >
        <div className="flex items-start justify-between mb-4">
          <h2 className="font-display text-lg font-semibold text-text">Provision a system account</h2>
          <button onClick={onClose} className="text-text-faint hover:text-text transition-colors"><X size={16} /></button>
        </div>
        <form onSubmit={submit} className="flex flex-col gap-3">
          <input required value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="Full name" className="bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none" />
          <input required type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} placeholder="Email" className="bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none" />
          <input value={form.phone_number} onChange={(e) => setForm({ ...form, phone_number: e.target.value })} placeholder="Phone (optional)" className="bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none" />
          <select value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })} className="bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text focus:border-brass/50 focus:outline-none">
            {ROLE_OPTIONS.map((r) => <option key={r} value={r}>{r}</option>)}
          </select>
          <input value={form.department} onChange={(e) => setForm({ ...form, department: e.target.value })} placeholder="Department (optional)" className="bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none" />
          <input value={form.department_role} onChange={(e) => setForm({ ...form, department_role: e.target.value })} placeholder="Title/role in department (optional)" className="bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none" />
          <input required type="password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} placeholder="Initial password" className="bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none" />
          {error && <div className="bg-rust/10 border border-rust/30 text-rust-soft text-[12px] rounded-[3px] px-3 py-2.5">{error}</div>}
          <button type="submit" disabled={submitting} className="bg-brass hover:bg-brass-soft disabled:opacity-60 text-ink font-medium text-[13px] rounded-[3px] py-2.5 transition-colors">
            {submitting ? "Creating…" : "Create account"}
          </button>
        </form>
      </motion.div>
    </AnimatePresence>
  );
}

function ResetPasswordModal({ target, onClose, onDone }: { target: UserRow | null; onClose: () => void; onDone: () => void }) {
  const [newPassword, setNewPassword] = useState("");
  const [adminPassword, setAdminPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    setNewPassword("");
    setAdminPassword("");
    setError(null);
  }, [target]);

  if (!target) return null;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await usersApi.resetPassword(target.id, newPassword, adminPassword);
      onDone();
    } catch (err) {
      setError(errMsg(err, "Couldn't reset that password."));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <AnimatePresence>
      <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onClick={onClose} className="fixed inset-0 bg-ink/70 backdrop-blur-sm z-40" />
      <motion.div initial={{ opacity: 0, y: 12, scale: 0.98 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, y: 8, scale: 0.98 }} className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-50 w-full max-w-sm bg-surface border border-border-soft rounded-[4px] p-6">
        <div className="flex items-start justify-between mb-1">
          <h2 className="font-display text-lg font-semibold text-text">Reset password</h2>
          <button onClick={onClose} className="text-text-faint hover:text-text transition-colors"><X size={16} /></button>
        </div>
        <p className="text-[12.5px] text-text-muted mb-4">for {target.name}</p>
        <form onSubmit={submit} className="flex flex-col gap-3">
          <input required type="password" value={newPassword} onChange={(e) => setNewPassword(e.target.value)} placeholder="New password" className="bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none" />
          <input required type="password" value={adminPassword} onChange={(e) => setAdminPassword(e.target.value)} placeholder="Your current password" className="bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none" />
          {error && <div className="bg-rust/10 border border-rust/30 text-rust-soft text-[12px] rounded-[3px] px-3 py-2.5">{error}</div>}
          <button type="submit" disabled={submitting} className="bg-brass hover:bg-brass-soft disabled:opacity-60 text-ink font-medium text-[13px] rounded-[3px] py-2.5 transition-colors">
            {submitting ? "Resetting…" : "Reset password"}
          </button>
        </form>
      </motion.div>
    </AnimatePresence>
  );
}

function UsersPanel({ canManage }: { canManage: boolean }) {
  const [rows, setRows] = useState<UserRow[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [resetting, setResetting] = useState<UserRow | null>(null);
  const [custody, setCustody] = useState<{ type: "user" | "outsider"; id: number; name: string } | null>(null);

  const refresh = () => {
    setLoading(true);
    usersApi.list(PAGE_SIZE, offset, search).then((res) => {
      setRows(res.items);
      setTotal(res.total);
      setLoading(false);
    });
  };

  useEffect(refresh, [offset, search]);

  const remove = async (u: UserRow) => {
    if (!confirm(`Delete ${u.name}'s account? It can be restored later from the Deleted list.`)) return;
    await usersApi.remove(u.id);
    refresh();
  };

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-2">
        <div className="relative flex-1 max-w-xs">
          <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-text-faint" />
          <input value={search} onChange={(e) => { setOffset(0); setSearch(e.target.value); }} placeholder="Search directory…" className="w-full bg-surface border border-border-soft rounded-[3px] pl-7 pr-3 py-2 text-[12.5px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none" />
        </div>
        {canManage && (
          <button onClick={() => setCreating(true)} className="flex items-center gap-1.5 bg-brass hover:bg-brass-soft text-ink font-medium text-[12px] rounded-[3px] px-3 py-2 transition-colors ml-auto">
            <Plus size={12} /> New account
          </button>
        )}
      </div>

      <div className="border border-border-soft bg-surface rounded-[3px] overflow-hidden">
        <table className="w-full text-left text-[12.5px]">
          <thead className="bg-surface-raised text-text-faint text-[11px] uppercase tracking-wide">
            <tr>
              <th className="px-5 py-3 font-medium">Name</th>
              <th className="hidden sm:table-cell px-5 py-3 font-medium">Role</th>
              <th className="hidden sm:table-cell px-5 py-3 font-medium">Custody</th>
              <th className="px-5 py-3 font-medium text-right">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border-soft">
            {loading && <tr><td colSpan={4} className="px-5 py-6 text-center text-text-faint">Loading…</td></tr>}
            {!loading && rows.length === 0 && <tr><td colSpan={4} className="px-5 py-8 text-center text-text-faint">No accounts match.</td></tr>}
            {rows.map((u) => (
              <tr key={u.id}>
                <td className="px-5 py-3">
                  <p className="text-text font-medium flex items-center">{u.name}<AlertDots alerts={u.alerts} /></p>
                  <p className="text-[11px] text-text-faint">{u.email}</p>
                </td>
                <td className="hidden sm:table-cell px-5 py-3 text-text-muted capitalize">{u.role.replace("_", " ")}{u.department_role ? ` · ${u.department_role}` : ""}</td>
                <td className="hidden sm:table-cell px-5 py-3 font-mono text-text-muted">{u.checkout_count}</td>
                <td className="px-5 py-3 text-right">
                  <div className="flex items-center justify-end gap-1.5 flex-wrap">
                    <button onClick={() => setCustody({ type: "user", id: u.id, name: u.name })} className="rounded-md border border-border-soft px-2 py-1 text-[11px] font-medium text-text-muted hover:border-sky/50 hover:text-sky transition-colors">Custody</button>
                    {canManage && <button onClick={() => setResetting(u)} title="Reset password" className="rounded-md border border-border-soft px-2 py-1 text-[11px] font-medium text-text-muted hover:border-brass/50 hover:text-brass-soft transition-colors"><KeyRound size={11} /></button>}
                    {canManage && <button onClick={() => remove(u)} title="Delete" className="rounded-md border border-border-soft px-2 py-1 text-[11px] font-medium text-text-muted hover:border-rust/50 hover:text-rust-soft transition-colors"><Trash2 size={11} /></button>}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {total > PAGE_SIZE && (
        <div className="flex items-center justify-between text-[12px] text-text-muted">
          <span>{offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of {total}</span>
          <div className="flex gap-2">
            <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))} className="border border-border-soft rounded-[3px] px-2.5 py-1 disabled:opacity-40">Prev</button>
            <button disabled={offset + PAGE_SIZE >= total} onClick={() => setOffset(offset + PAGE_SIZE)} className="border border-border-soft rounded-[3px] px-2.5 py-1 disabled:opacity-40">Next</button>
          </div>
        </div>
      )}

      <CreateUserModal open={creating} onClose={() => setCreating(false)} onCreated={() => { setCreating(false); refresh(); }} />
      <ResetPasswordModal target={resetting} onClose={() => setResetting(null)} onDone={() => { setResetting(null); alert("Password reset."); }} />
      <CustodyDrawer target={custody} onClose={() => setCustody(null)} />
    </div>
  );
}

// =============================================================================
// Ad-Hoc (Unlinked) Directory -- ported from js/components/outsiders.js.
// External individuals dispatched equipment without ever holding a login.
// =============================================================================

function OutsidersPanel({ canManage }: { canManage: boolean }) {
  const [rows, setRows] = useState<OutsiderRow[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [custody, setCustody] = useState<{ type: "user" | "outsider"; id: number; name: string } | null>(null);

  const refresh = () => {
    setLoading(true);
    outsidersApi.list(PAGE_SIZE, offset, search).then((res) => {
      setRows(res.items);
      setTotal(res.total);
      setLoading(false);
    });
  };

  useEffect(refresh, [offset, search]);

  const remove = async (o: OutsiderRow) => {
    if (!confirm(`Delete ${o.name}'s ad-hoc profile? Their historical checkout records are kept.`)) return;
    await outsidersApi.remove(o.id);
    refresh();
  };

  return (
    <div className="flex flex-col gap-4">
      <div className="relative max-w-xs">
        <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-text-faint" />
        <input value={search} onChange={(e) => { setOffset(0); setSearch(e.target.value); }} placeholder="Search ad-hoc profiles…" className="w-full bg-surface border border-border-soft rounded-[3px] pl-7 pr-3 py-2 text-[12.5px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none" />
      </div>

      <div className="border border-border-soft bg-surface rounded-[3px] overflow-hidden">
        <table className="w-full text-left text-[12.5px]">
          <thead className="bg-surface-raised text-text-faint text-[11px] uppercase tracking-wide">
            <tr>
              <th className="px-5 py-3 font-medium">Name</th>
              <th className="hidden sm:table-cell px-5 py-3 font-medium">Company</th>
              <th className="hidden sm:table-cell px-5 py-3 font-medium">Custody</th>
              <th className="px-5 py-3 font-medium text-right">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border-soft">
            {loading && <tr><td colSpan={4} className="px-5 py-6 text-center text-text-faint">Loading…</td></tr>}
            {!loading && rows.length === 0 && <tr><td colSpan={4} className="px-5 py-8 text-center text-text-faint">No ad-hoc profiles match.</td></tr>}
            {rows.map((o) => (
              <tr key={o.id}>
                <td className="px-5 py-3">
                  <p className="text-text font-medium flex items-center">{o.name}<AlertDots alerts={o.alerts} /></p>
                  <p className="text-[11px] text-text-faint">{o.email || "—"}</p>
                </td>
                <td className="hidden sm:table-cell px-5 py-3 text-text-muted">{o.company || "—"}</td>
                <td className="hidden sm:table-cell px-5 py-3 font-mono text-text-muted">{o.outstanding_items}</td>
                <td className="px-5 py-3 text-right">
                  <div className="flex items-center justify-end gap-1.5">
                    <button onClick={() => setCustody({ type: "outsider", id: o.id, name: o.name })} className="rounded-md border border-border-soft px-2 py-1 text-[11px] font-medium text-text-muted hover:border-sky/50 hover:text-sky transition-colors">Custody</button>
                    {canManage && <button onClick={() => remove(o)} title="Delete" className="rounded-md border border-border-soft px-2 py-1 text-[11px] font-medium text-text-muted hover:border-rust/50 hover:text-rust-soft transition-colors"><Trash2 size={11} /></button>}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {total > PAGE_SIZE && (
        <div className="flex items-center justify-between text-[12px] text-text-muted">
          <span>{offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of {total}</span>
          <div className="flex gap-2">
            <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))} className="border border-border-soft rounded-[3px] px-2.5 py-1 disabled:opacity-40">Prev</button>
            <button disabled={offset + PAGE_SIZE >= total} onClick={() => setOffset(offset + PAGE_SIZE)} className="border border-border-soft rounded-[3px] px-2.5 py-1 disabled:opacity-40">Next</button>
          </div>
        </div>
      )}

      <CustodyDrawer target={custody} onClose={() => setCustody(null)} />
    </div>
  );
}

// =============================================================================
// Audit Trail -- ported from js/components/audit.js. Export runs async on a
// Celery worker: enqueue -> poll status every ~1.5s -> download when ready.
// =============================================================================

function AuditPanel() {
  const [rows, setRows] = useState<AuditLogEntry[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [exporting, setExporting] = useState<"csv" | "pdf" | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true);
    auditApi.list(PAGE_SIZE, offset).then((res) => {
      setRows(res.items);
      setTotal(res.total);
      setLoading(false);
    });
  }, [offset]);

  const runExport = async (format: "csv" | "pdf") => {
    setExporting(format);
    setExportError(null);
    try {
      const { task_id } = await auditApi.startExport(format);
      const deadline = Date.now() + 60_000;
      while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 1500));
        const status = await auditApi.exportStatus(task_id);
        if (status.state === "SUCCESS") {
          const a = document.createElement("a");
          a.href = auditApi.downloadUrl(task_id);
          a.download = `audit_log_export.${format}`;
          a.click();
          return;
        }
        if (status.state === "FAILURE") throw new Error(status.error || "Export job failed.");
      }
      throw new Error("Export is taking longer than expected -- try again shortly.");
    } catch (err) {
      setExportError(errMsg(err, "Export failed."));
    } finally {
      setExporting(null);
    }
  };

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-2">
        <p className="text-[12.5px] text-text-muted">{total} entries in the ledger</p>
        <div className="ml-auto flex gap-2">
          <button onClick={() => runExport("csv")} disabled={!!exporting} className="flex items-center gap-1.5 border border-border-soft hover:border-brass/40 disabled:opacity-60 text-[12px] text-text rounded-[3px] px-3 py-1.5 transition-colors">
            {exporting === "csv" ? <Loader2 size={12} className="animate-spin" /> : <Download size={12} />} Export CSV
          </button>
          <button onClick={() => runExport("pdf")} disabled={!!exporting} className="flex items-center gap-1.5 border border-border-soft hover:border-brass/40 disabled:opacity-60 text-[12px] text-text rounded-[3px] px-3 py-1.5 transition-colors">
            {exporting === "pdf" ? <Loader2 size={12} className="animate-spin" /> : <Download size={12} />} Export PDF
          </button>
        </div>
      </div>
      {exportError && <div className="bg-rust/10 border border-rust/30 text-rust-soft text-[12px] rounded-[3px] px-3 py-2.5">{exportError}</div>}

      <div className="border border-border-soft bg-surface rounded-[3px] overflow-hidden">
        <table className="w-full text-left text-[12.5px]">
          <thead className="bg-surface-raised text-text-faint text-[11px] uppercase tracking-wide">
            <tr>
              <th className="px-5 py-3 font-medium">Timestamp</th>
              <th className="px-5 py-3 font-medium">Operator</th>
              <th className="hidden sm:table-cell px-5 py-3 font-medium">Action</th>
              <th className="hidden sm:table-cell px-5 py-3 font-medium">Details</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border-soft">
            {loading && <tr><td colSpan={4} className="px-5 py-6 text-center text-text-faint">Loading…</td></tr>}
            {!loading && rows.length === 0 && <tr><td colSpan={4} className="px-5 py-8 text-center text-text-faint">No entries.</td></tr>}
            {rows.map((r) => (
              <tr key={r.id}>
                <td className="px-5 py-3 font-mono text-text-muted whitespace-nowrap">{new Date(r.timestamp).toLocaleString()}</td>
                <td className="px-5 py-3 text-text-muted">{r.operator}</td>
                <td className="hidden sm:table-cell px-5 py-3 text-text font-medium">{r.action}</td>
                <td className="hidden sm:table-cell px-5 py-3 text-text-muted">{r.details}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {total > PAGE_SIZE && (
        <div className="flex items-center justify-between text-[12px] text-text-muted">
          <span>{offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of {total}</span>
          <div className="flex gap-2">
            <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))} className="border border-border-soft rounded-[3px] px-2.5 py-1 disabled:opacity-40">Prev</button>
            <button disabled={offset + PAGE_SIZE >= total} onClick={() => setOffset(offset + PAGE_SIZE)} className="border border-border-soft rounded-[3px] px-2.5 py-1 disabled:opacity-40">Next</button>
          </div>
        </div>
      )}
    </div>
  );
}

// =============================================================================
// Admin -- tabbed entry point. Each tab is hidden unless the signed-in
// role (or demo mode, for exploration) actually clears the matching
// backend gate; see lib/roles.ts.
// =============================================================================

export function Admin() {
  const { user, demo } = useAuth();
  const canImport = demo || isFullAdmin(user?.role);
  const canBackups = demo || isTrueSuperAdmin(user?.role);
  const canDirectory = demo || isPrivileged(user?.role);
  const canManageAccounts = demo || isTrueSuperAdmin(user?.role);

  type Tab = { key: "import" | "backups" | "users" | "outsiders" | "audit"; label: string; icon: typeof FileSpreadsheet };
  const tabs = useMemo<Tab[]>(() => {
    const list: Tab[] = [];
    if (canDirectory) list.push({ key: "users", label: "User Directory", icon: UsersIcon });
    if (canDirectory) list.push({ key: "outsiders", label: "Ad-Hoc Directory", icon: Contact });
    if (canDirectory) list.push({ key: "audit", label: "Audit Trail", icon: ScrollText });
    if (canImport) list.push({ key: "import", label: "Inventory Import", icon: FileSpreadsheet });
    if (canBackups) list.push({ key: "backups", label: "System Backups", icon: DatabaseBackup });
    return list;
  }, [canImport, canBackups, canDirectory]);

  const [tab, setTab] = useState<Tab["key"] | null>(tabs[0]?.key ?? null);
  useEffect(() => {
    if (!tabs.some((t) => t.key === tab)) setTab(tabs[0]?.key ?? null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tabs]);

  return (
    <div>
      <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }} className="mb-6">
        <h1 className="font-display text-2xl font-semibold text-text">Admin</h1>
        <p className="text-text-muted text-sm mt-1">Directories, audit trail, inventory import, and system-level backup controls.</p>
      </motion.div>

      {tabs.length === 0 ? (
        <div className="flex flex-col items-center justify-center text-center py-20 border border-border-soft rounded-[3px] bg-surface">
          <ShieldAlert size={20} className="text-text-faint mb-3" />
          <p className="text-[13px] text-text-muted max-w-sm">
            Your role doesn't include access to anything in Admin. Ask a Super Admin if you need something here.
          </p>
        </div>
      ) : (
        <>
          <div className="flex items-center gap-2 mb-5 flex-wrap">
            {tabs.map((t) => (
              <button
                key={t.key}
                onClick={() => setTab(t.key)}
                className={`flex items-center gap-1.5 px-3 py-1.5 rounded-full text-[12px] font-medium border transition-colors ${
                  tab === t.key ? "bg-brass/15 border-brass/40 text-brass-soft" : "border-border-soft text-text-muted hover:text-text hover:border-border"
                }`}
              >
                <t.icon size={12} /> {t.label}
              </button>
            ))}
          </div>

          {tab === "users" && canDirectory && <UsersPanel canManage={canManageAccounts} />}
          {tab === "outsiders" && canDirectory && <OutsidersPanel canManage={canDirectory} />}
          {tab === "audit" && canDirectory && <AuditPanel />}
          {tab === "import" && canImport && <InventoryImportPanel />}
          {tab === "backups" && canBackups && <SystemBackupsPanel />}
        </>
      )}
    </div>
  );
}
