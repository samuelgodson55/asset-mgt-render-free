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
  FileText,
  CheckCircle,
  PackageCheck,
  Boxes,
  Percent,
  Pencil,
  UserMinus,
  ArrowRightLeft,
} from "lucide-react";
import { useAuth } from "../lib/useAuth";
import { useNavigate, useSearchParams } from "react-router-dom";
import { backupApi, digestApi, importApi, usersApi, outsidersApi, auditApi, quotationsApi, assetsApi, ApiError, formatPrice, formatDate } from "../lib/api";
import type { BackupEntry, BackupStatus, ImportResult, RestoreResult, UserRow, OutsiderRow, AuditLogEntry, QuotationListRow, CatalogAsset, FulfillmentQueueRow, QuotationLineItem, QuotationOutsourceShortfallItem, DeletedAssetRow, DeletedUserRow } from "../lib/types";
import { isFullAdmin, isTrueSuperAdmin, isPrivileged, canManageUserRole } from "../lib/roles";
import { CustodyDrawer } from "../components/CustodyDrawer";
import { QuoteDetailDrawer } from "../components/QuoteDetailDrawer";
import { ExportButtons } from "../components/ExportButtons";
import { PaginationBar, RowsPerPageSelect } from "../components/PaginationBar";
import { DEFAULT_PAGE_SIZE } from "../lib/pagination";

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
      <motion.div key="backdrop" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onClick={onClose} className="fixed inset-0 bg-ink/70 backdrop-blur-sm z-40" />
      <motion.div key="panel"
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
      <motion.div key="backdrop" initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="fixed inset-0 bg-ink/80 backdrop-blur-sm z-40" />
      <motion.div
        key="panel"
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
    digestApi.list()
      .then((e) => {
        setEmails(e);
        setLoading(false);
      })
      .catch((err) => {
        console.error("Failed to load digest recipients:", err);
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
    try {
      const [s, list] = await Promise.all([backupApi.status(), backupApi.list()]);
      setStatus(s);
      setBackups(list);
    } catch (err) {
      console.error("Failed to load backup status/list:", err);
    } finally {
      setLoadingList(false);
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
// Quotation Settings -- ported from the legacy frontend's admin.html
// "Quotation Settings" card (js/components/quotation.js's
// loadVatSetting()/submitVatSettingsForm()). PUT /settings/vat is
// require_super_admin -- Super Admin AND a plain Admin account; GET is open
// to any authenticated user, but this panel itself is never shown outside
// Admin, so that distinction doesn't need its own gate here.
// =============================================================================

function SettingsPanel() {
  const [vatPercent, setVatPercent] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<{ text: string; ok: boolean } | null>(null);

  useEffect(() => {
    quotationsApi.getVat().then((data) => setVatPercent(String(data.vat_percent))).finally(() => setLoading(false));
  }, []);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const parsed = parseFloat(vatPercent);
    if (isNaN(parsed) || parsed < 0 || parsed > 100) {
      setMessage({ text: "Enter a VAT percentage between 0 and 100.", ok: false });
      return;
    }
    setMessage(null);
    setSaving(true);
    try {
      const data = await quotationsApi.setVat(parsed);
      setVatPercent(String(data.vat_percent));
      setMessage({ text: "VAT updated -- applies to every saved order immediately.", ok: true });
    } catch (err) {
      setMessage({ text: errMsg(err, "Couldn't update VAT."), ok: false });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="max-w-sm">
      <div className="border border-border-soft bg-surface rounded-[3px] p-5">
        <div className="flex items-center gap-2 mb-1">
          <Percent size={14} className="text-brass-soft" />
          <h2 className="font-display text-[14px] font-semibold text-text">Quotation Settings</h2>
        </div>
        <p className="text-[12px] text-text-muted mb-4">The global VAT percentage applied to every Quotation's total.</p>

        {loading ? (
          <p className="text-[12px] text-text-faint">Loading…</p>
        ) : (
          <form onSubmit={submit} className="flex flex-col gap-3">
            <label className="block">
              <span className="text-[11px] uppercase tracking-wider text-text-faint">VAT percent</span>
              <input
                type="number"
                min={0}
                max={100}
                step={0.01}
                value={vatPercent}
                onChange={(e) => setVatPercent(e.target.value)}
                className="w-full mt-1.5 bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text focus:border-brass/50 focus:outline-none transition-colors"
              />
            </label>
            {message && (
              <p className={`text-[12px] font-medium ${message.ok ? "text-moss-soft" : "text-rust-soft"}`}>{message.text}</p>
            )}
            <button type="submit" disabled={saving} className="bg-brass hover:bg-brass-soft disabled:opacity-60 text-ink font-medium text-[13px] rounded-[3px] py-2.5 transition-colors">
              {saving ? "Saving…" : "Save VAT setting"}
            </button>
          </form>
        )}
      </div>
    </div>
  );
}

// =============================================================================
// User Directory -- ported from js/components/users.js. List/search/custody
// is require_privileged_role; reset-password/delete/restore are
// require_super_admin -- Super Admin AND a plain Admin account alike (see
// deps.py's _FULL_ADMIN_ROLES), so those affordances are hidden only for a
// Manager, who can see the directory itself but not manage accounts in it.
// =============================================================================

const ROLE_OPTIONS = ["staff", "manager", "admin", "customer"];

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

function CreateUserModal({ open, onClose, onCreated, roleOptions }: { open: boolean; onClose: () => void; onCreated: () => void; roleOptions: string[] }) {
  const [form, setForm] = useState({ name: "", email: "", phone_number: "", role: roleOptions[0] ?? "staff", password: "", department: "", department_role: "" });
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (open) {
      setForm({ name: "", email: "", phone_number: "", role: roleOptions[0] ?? "staff", password: "", department: "", department_role: "" });
      setError(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
      <motion.div key="backdrop" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onClick={onClose} className="fixed inset-0 bg-ink/70 backdrop-blur-sm z-40" />
      <motion.div key="panel"
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
            {roleOptions.map((r) => <option key={r} value={r}>{r}</option>)}
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
      <motion.div key="backdrop" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onClick={onClose} className="fixed inset-0 bg-ink/70 backdrop-blur-sm z-40" />
      <motion.div key="panel" initial={{ opacity: 0, y: 12, scale: 0.98 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, y: 8, scale: 0.98 }} className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-50 w-full max-w-sm bg-surface border border-border-soft rounded-[4px] p-6">
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

function EditUserModal({ target, onClose, onDone }: { target: UserRow | null; onClose: () => void; onDone: () => void }) {
  const [form, setForm] = useState({ name: "", username: "", email: "", phone_number: "" });
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (target) {
      setForm({ name: target.name, username: target.username ?? "", email: target.email, phone_number: target.phone_number ?? "" });
      setError(null);
    }
  }, [target]);

  if (!target) return null;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await usersApi.update(target.id, form);
      onDone();
    } catch (err) {
      setError(errMsg(err, "Couldn't save those changes."));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <AnimatePresence>
      <motion.div key="backdrop" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onClick={onClose} className="fixed inset-0 bg-ink/70 backdrop-blur-sm z-40" />
      <motion.div key="panel" initial={{ opacity: 0, y: 12, scale: 0.98 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, y: 8, scale: 0.98 }} className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-50 w-full max-w-sm bg-surface border border-border-soft rounded-[4px] p-6">
        <div className="flex items-start justify-between mb-4">
          <h2 className="font-display text-lg font-semibold text-text">Edit account</h2>
          <button onClick={onClose} className="text-text-faint hover:text-text transition-colors"><X size={16} /></button>
        </div>
        <form onSubmit={submit} className="flex flex-col gap-3">
          <input required value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="Full name" className="bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none" />
          <input value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} placeholder="Username (optional)" className="bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none" />
          <input required type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} placeholder="Email" className="bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none" />
          <input value={form.phone_number} onChange={(e) => setForm({ ...form, phone_number: e.target.value })} placeholder="Phone (optional)" className="bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none" />
          {error && <div className="bg-rust/10 border border-rust/30 text-rust-soft text-[12px] rounded-[3px] px-3 py-2.5">{error}</div>}
          <button type="submit" disabled={submitting} className="bg-brass hover:bg-brass-soft disabled:opacity-60 text-ink font-medium text-[13px] rounded-[3px] py-2.5 transition-colors">
            {submitting ? "Saving…" : "Save changes"}
          </button>
        </form>
      </motion.div>
    </AnimatePresence>
  );
}

function RevokeUserModal({ target, onClose, onDone }: { target: UserRow | null; onClose: () => void; onDone: () => void }) {
  const [form, setForm] = useState({ email: "", phone_number: "", company: "" });
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (target) {
      setForm({ email: "", phone_number: "", company: "" });
      setError(null);
    }
  }, [target]);

  if (!target) return null;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!confirm(`Revoke ${target.name}'s login access? They'll become an unlinked Ad-Hoc profile -- their custody history moves with them, but they can no longer sign in.`)) return;
    setSubmitting(true);
    setError(null);
    try {
      const req: Partial<{ email: string; phone_number: string; company: string }> = {};
      if (form.email.trim()) req.email = form.email.trim();
      if (form.phone_number.trim()) req.phone_number = form.phone_number.trim();
      if (form.company.trim()) req.company = form.company.trim();
      await usersApi.convertToOutsider(target.id, req);
      onDone();
    } catch (err) {
      setError(errMsg(err, "Couldn't revoke access."));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <AnimatePresence>
      <motion.div key="backdrop" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onClick={onClose} className="fixed inset-0 bg-ink/70 backdrop-blur-sm z-40" />
      <motion.div key="panel" initial={{ opacity: 0, y: 12, scale: 0.98 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, y: 8, scale: 0.98 }} className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-50 w-full max-w-sm bg-surface border border-border-soft rounded-[4px] p-6">
        <div className="flex items-start justify-between mb-1">
          <h2 className="font-display text-lg font-semibold text-text">Revoke access</h2>
          <button onClick={onClose} className="text-text-faint hover:text-text transition-colors"><X size={16} /></button>
        </div>
        <p className="text-[12.5px] text-text-muted mb-4">Convert {target.name}'s account into an unlinked Ad-Hoc profile. Fields left blank keep the account's current email/phone.</p>
        <form onSubmit={submit} className="flex flex-col gap-3">
          <input type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} placeholder={`Email (default: ${target.email})`} className="bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none" />
          <input value={form.phone_number} onChange={(e) => setForm({ ...form, phone_number: e.target.value })} placeholder="Phone (optional)" className="bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none" />
          <input value={form.company} onChange={(e) => setForm({ ...form, company: e.target.value })} placeholder="Company (optional)" className="bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none" />
          {error && <div className="bg-rust/10 border border-rust/30 text-rust-soft text-[12px] rounded-[3px] px-3 py-2.5">{error}</div>}
          <button type="submit" disabled={submitting} className="bg-rust hover:bg-rust-soft disabled:opacity-60 text-ink font-medium text-[13px] rounded-[3px] py-2.5 transition-colors">
            {submitting ? "Revoking…" : "Revoke access"}
          </button>
        </form>
      </motion.div>
    </AnimatePresence>
  );
}

function UsersPanel({
  canManage,
  canCreate,
  actorRole,
  demo,
  openCustody: deepLinkCustody,
  onOpenedCustody,
}: {
  /** Super Admin/Admin only -- gates Reset Password and Delete Profile,
   * both require_super_admin on the backend with no Manager exception. */
  canManage: boolean;
  /** Super Admin/Admin (any role), OR a Manager (staff/customer roles
   * only, enforced by CreateUserModal's roleOptions below) -- gates the
   * "New account" button itself. Mirrors POST /users' require_privileged_role. */
  canCreate: boolean;
  actorRole: string | undefined | null;
  demo: boolean;
  openCustody?: { id: number; name: string } | null;
  onOpenedCustody?: () => void;
}) {
  const [rows, setRows] = useState<UserRow[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [perPage, setPerPage] = useState(DEFAULT_PAGE_SIZE);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [resetting, setResetting] = useState<UserRow | null>(null);
  const [editing, setEditing] = useState<UserRow | null>(null);
  const [revoking, setRevoking] = useState<UserRow | null>(null);
  const [custody, setCustody] = useState<{ type: "user" | "outsider"; id: number; name: string } | null>(null);

  // Edit / Revoke Access per row: full admins act on anyone; a Manager
  // only on a "staff"/"customer" row -- mirrors services/user_service.py's
  // update_user()/convert_user_to_outsider() MANAGER_PROVISIONABLE_ROLES
  // check, so a Manager is never shown a button that would just come
  // back as a 403.
  const canEditRow = (targetRole: string) => canManageUserRole(actorRole, targetRole, demo);

  // Managers may only ever provision a "staff" or "customer" account --
  // mirrors manager.html's Provision form, which never even offers a
  // Manager/Admin option, and services/user_service.py's own enforcement
  // of the same limit.
  const createRoleOptions = demo || isFullAdmin(actorRole) ? ROLE_OPTIONS : ["staff", "customer"];

  // Deep link from the Notification Bell (?custody=user:ID&name=...) --
  // opens the Custody Ledger drawer straight away, same click-through as
  // legacy notifications.js's data-action="open-custody" rows.
  useEffect(() => {
    if (deepLinkCustody) {
      setCustody({ type: "user", id: deepLinkCustody.id, name: deepLinkCustody.name });
      onOpenedCustody?.();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [deepLinkCustody]);

  const refresh = () => {
    setLoading(true);
    usersApi.list(perPage, offset, search).then((res) => {
      setRows(res.items);
      setTotal(res.total);
      setLoading(false);
    });
  };

  useEffect(refresh, [offset, perPage, search]);

  const handlePerPageChange = (n: number) => {
    setPerPage(n);
    setOffset(0);
  };

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
        <div className="ml-auto flex items-center gap-2">
          <RowsPerPageSelect value={perPage} onChange={handlePerPageChange} />
          <ExportButtons
            compact
            disabled={total === 0}
            urlFor={(format) => usersApi.exportUrl(format)}
            filenameFor={(format) => `all_users_properties.${format}`}
          />
          {canCreate && (
            <button onClick={() => setCreating(true)} className="flex items-center gap-1.5 bg-brass hover:bg-brass-soft text-ink font-medium text-[12px] rounded-[3px] px-3 py-2 transition-colors">
              <Plus size={12} /> New account
            </button>
          )}
        </div>
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
                    {canEditRow(u.role) && <button onClick={() => setEditing(u)} title="Edit" className="rounded-md border border-border-soft px-2 py-1 text-[11px] font-medium text-text-muted hover:border-brass/50 hover:text-brass-soft transition-colors"><Pencil size={11} /></button>}
                    {canManage && <button onClick={() => setResetting(u)} title="Reset password" className="rounded-md border border-border-soft px-2 py-1 text-[11px] font-medium text-text-muted hover:border-brass/50 hover:text-brass-soft transition-colors"><KeyRound size={11} /></button>}
                    {canEditRow(u.role) && <button onClick={() => setRevoking(u)} title="Revoke access (convert to Ad-Hoc)" className="rounded-md border border-border-soft px-2 py-1 text-[11px] font-medium text-text-muted hover:border-rust/50 hover:text-rust-soft transition-colors"><UserMinus size={11} /></button>}
                    {canManage && <button onClick={() => remove(u)} title="Delete" className="rounded-md border border-border-soft px-2 py-1 text-[11px] font-medium text-text-muted hover:border-rust/50 hover:text-rust-soft transition-colors"><Trash2 size={11} /></button>}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <PaginationBar total={total} perPage={perPage} offset={offset} onOffsetChange={setOffset} />

      <CreateUserModal open={creating} onClose={() => setCreating(false)} onCreated={() => { setCreating(false); refresh(); }} roleOptions={createRoleOptions} />
      <ResetPasswordModal target={resetting} onClose={() => setResetting(null)} onDone={() => { setResetting(null); alert("Password reset."); }} />
      <EditUserModal target={editing} onClose={() => setEditing(null)} onDone={() => { setEditing(null); refresh(); }} />
      <RevokeUserModal target={revoking} onClose={() => setRevoking(null)} onDone={() => { setRevoking(null); refresh(); }} />
      <CustodyDrawer target={custody} onClose={() => setCustody(null)} />
    </div>
  );
}

// =============================================================================
// Ad-Hoc (Unlinked) Directory -- ported from js/components/outsiders.js.
// External individuals dispatched equipment without ever holding a login.
// =============================================================================

function EditOutsiderModal({ target, onClose, onDone }: { target: OutsiderRow | null; onClose: () => void; onDone: () => void }) {
  const [form, setForm] = useState({ name: "", email: "", phone_number: "", company: "" });
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (target) {
      setForm({ name: target.name, email: target.email ?? "", phone_number: target.phone_number ?? "", company: target.company ?? "" });
      setError(null);
    }
  }, [target]);

  if (!target) return null;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await outsidersApi.update(target.id, form);
      onDone();
    } catch (err) {
      setError(errMsg(err, "Couldn't save those changes."));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <AnimatePresence>
      <motion.div key="backdrop" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onClick={onClose} className="fixed inset-0 bg-ink/70 backdrop-blur-sm z-40" />
      <motion.div key="panel" initial={{ opacity: 0, y: 12, scale: 0.98 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, y: 8, scale: 0.98 }} className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-50 w-full max-w-sm bg-surface border border-border-soft rounded-[4px] p-6">
        <div className="flex items-start justify-between mb-4">
          <h2 className="font-display text-lg font-semibold text-text">Edit ad-hoc profile</h2>
          <button onClick={onClose} className="text-text-faint hover:text-text transition-colors"><X size={16} /></button>
        </div>
        <form onSubmit={submit} className="flex flex-col gap-3">
          <input required value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="Full name" className="bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none" />
          <input type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} placeholder="Email (optional)" className="bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none" />
          <input value={form.phone_number} onChange={(e) => setForm({ ...form, phone_number: e.target.value })} placeholder="Phone (optional)" className="bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none" />
          <input value={form.company} onChange={(e) => setForm({ ...form, company: e.target.value })} placeholder="Company (optional)" className="bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none" />
          {error && <div className="bg-rust/10 border border-rust/30 text-rust-soft text-[12px] rounded-[3px] px-3 py-2.5">{error}</div>}
          <button type="submit" disabled={submitting} className="bg-brass hover:bg-brass-soft disabled:opacity-60 text-ink font-medium text-[13px] rounded-[3px] py-2.5 transition-colors">
            {submitting ? "Saving…" : "Save changes"}
          </button>
        </form>
      </motion.div>
    </AnimatePresence>
  );
}

function ConvertOutsiderModal({ target, onClose, onDone, roleOptions }: { target: OutsiderRow | null; onClose: () => void; onDone: () => void; roleOptions: string[] }) {
  const [form, setForm] = useState({ email: "", phone_number: "", password: "", role: roleOptions[0] ?? "staff", department: "", department_role: "" });
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (target) {
      setForm({ email: target.email ?? "", phone_number: target.phone_number ?? "", password: "", role: roleOptions[0] ?? "staff", department: "", department_role: "" });
      setError(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target]);

  if (!target) return null;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await outsidersApi.convertToUser(target.id, form);
      onDone();
    } catch (err) {
      setError(errMsg(err, "Couldn't convert this profile."));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <AnimatePresence>
      <motion.div key="backdrop" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onClick={onClose} className="fixed inset-0 bg-ink/70 backdrop-blur-sm z-40" />
      <motion.div key="panel" initial={{ opacity: 0, y: 12, scale: 0.98 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, y: 8, scale: 0.98 }} className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-50 w-full max-w-sm bg-surface border border-border-soft rounded-[4px] p-6 max-h-[85vh] overflow-y-auto">
        <div className="flex items-start justify-between mb-1">
          <h2 className="font-display text-lg font-semibold text-text">Convert to user</h2>
          <button onClick={onClose} className="text-text-faint hover:text-text transition-colors"><X size={16} /></button>
        </div>
        <p className="text-[12.5px] text-text-muted mb-4">Give {target.name} a real login account. Their name carries over from this profile.</p>
        <form onSubmit={submit} className="flex flex-col gap-3">
          <input required type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} placeholder="Email" className="bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none" />
          <input value={form.phone_number} onChange={(e) => setForm({ ...form, phone_number: e.target.value })} placeholder="Phone (optional)" className="bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none" />
          <select value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })} className="bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text focus:border-brass/50 focus:outline-none">
            {roleOptions.map((r) => <option key={r} value={r}>{r}</option>)}
          </select>
          <input value={form.department} onChange={(e) => setForm({ ...form, department: e.target.value })} placeholder="Department (optional)" className="bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none" />
          <input value={form.department_role} onChange={(e) => setForm({ ...form, department_role: e.target.value })} placeholder="Title/role in department (optional)" className="bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none" />
          <input required type="password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} placeholder="Initial password" className="bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none" />
          {error && <div className="bg-rust/10 border border-rust/30 text-rust-soft text-[12px] rounded-[3px] px-3 py-2.5">{error}</div>}
          <button type="submit" disabled={submitting} className="bg-brass hover:bg-brass-soft disabled:opacity-60 text-ink font-medium text-[13px] rounded-[3px] py-2.5 transition-colors">
            {submitting ? "Converting…" : "Convert to user"}
          </button>
        </form>
      </motion.div>
    </AnimatePresence>
  );
}

function OutsidersPanel({
  canManage,
  actorRole,
  demo,
  openCustody: deepLinkCustody,
  onOpenedCustody,
}: {
  canManage: boolean;
  actorRole: string | undefined | null;
  demo: boolean;
  openCustody?: { id: number; name: string } | null;
  onOpenedCustody?: () => void;
}) {
  const [rows, setRows] = useState<OutsiderRow[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [perPage, setPerPage] = useState(DEFAULT_PAGE_SIZE);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [custody, setCustody] = useState<{ type: "user" | "outsider"; id: number; name: string } | null>(null);
  const [editing, setEditing] = useState<OutsiderRow | null>(null);
  const [converting, setConverting] = useState<OutsiderRow | null>(null);

  // Same Manager role ceiling as UsersPanel's createRoleOptions above --
  // mirrors manager.html's "Convert to user" role select and
  // services/outsider_service.py's convert_outsider_to_user().
  const convertRoleOptions = demo || isFullAdmin(actorRole) ? ROLE_OPTIONS : ["staff", "customer"];

  useEffect(() => {
    if (deepLinkCustody) {
      setCustody({ type: "outsider", id: deepLinkCustody.id, name: deepLinkCustody.name });
      onOpenedCustody?.();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [deepLinkCustody]);

  const refresh = () => {
    setLoading(true);
    outsidersApi.list(perPage, offset, search).then((res) => {
      setRows(res.items);
      setTotal(res.total);
      setLoading(false);
    });
  };

  useEffect(refresh, [offset, perPage, search]);

  const handlePerPageChange = (n: number) => {
    setPerPage(n);
    setOffset(0);
  };

  const remove = async (o: OutsiderRow) => {
    if (!confirm(`Delete ${o.name}'s ad-hoc profile? Their historical checkout records are kept.`)) return;
    await outsidersApi.remove(o.id);
    refresh();
  };

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-2">
        <div className="relative max-w-xs flex-1">
          <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-text-faint" />
          <input value={search} onChange={(e) => { setOffset(0); setSearch(e.target.value); }} placeholder="Search ad-hoc profiles…" className="w-full bg-surface border border-border-soft rounded-[3px] pl-7 pr-3 py-2 text-[12.5px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none" />
        </div>
        <RowsPerPageSelect value={perPage} onChange={handlePerPageChange} />
        <ExportButtons
          compact
          disabled={total === 0}
          urlFor={(format) => outsidersApi.exportUrl(format)}
          filenameFor={(format) => `all_outsiders_properties.${format}`}
        />
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
                    {canManage && <button onClick={() => setEditing(o)} title="Edit" className="rounded-md border border-border-soft px-2 py-1 text-[11px] font-medium text-text-muted hover:border-brass/50 hover:text-brass-soft transition-colors"><Pencil size={11} /></button>}
                    {canManage && <button onClick={() => setConverting(o)} title="Convert to user" className="rounded-md border border-border-soft px-2 py-1 text-[11px] font-medium text-text-muted hover:border-moss/50 hover:text-moss-soft transition-colors"><ArrowRightLeft size={11} /></button>}
                    {canManage && <button onClick={() => remove(o)} title="Delete" className="rounded-md border border-border-soft px-2 py-1 text-[11px] font-medium text-text-muted hover:border-rust/50 hover:text-rust-soft transition-colors"><Trash2 size={11} /></button>}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <PaginationBar total={total} perPage={perPage} offset={offset} onOffsetChange={setOffset} />

      <CustodyDrawer target={custody} onClose={() => setCustody(null)} />
      <EditOutsiderModal target={editing} onClose={() => setEditing(null)} onDone={() => { setEditing(null); refresh(); }} />
      <ConvertOutsiderModal target={converting} onClose={() => setConverting(null)} onDone={() => { setConverting(null); refresh(); }} roleOptions={convertRoleOptions} />
    </div>
  );
}

// =============================================================================
// Restore Deleted Users -- ported from js/components/users.js's
// loadDeletedUsers()/restoreUser(). require_super_admin on GET /users/deleted
// and POST /users/{id}/restore -- Super Admin AND a plain Admin account,
// Admin page only -- never shown on the Manager page, same tier as
// System Backups (which, unlike this, stays Super-Admin-only).
// =============================================================================

function DeletedUsersPanel() {
  const [rows, setRows] = useState<DeletedUserRow[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [perPage, setPerPage] = useState(DEFAULT_PAGE_SIZE);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = () => {
    setLoading(true);
    setError(null);
    usersApi
      .listDeleted(perPage, offset, search)
      .then((res) => {
        setRows(res.items);
        setTotal(res.total);
      })
      .catch((err) => setError(errMsg(err, "Couldn't load deleted accounts.")))
      .finally(() => setLoading(false));
  };

  useEffect(refresh, [offset, perPage, search]);

  const handlePerPageChange = (n: number) => {
    setPerPage(n);
    setOffset(0);
  };

  const restore = async (u: DeletedUserRow) => {
    if (!confirm(`Restore ${u.name}'s account? Their login will work again and they'll reappear in the User Directory immediately.`)) return;
    setBusyId(u.id);
    try {
      await usersApi.restore(u.id);
      refresh();
    } catch (err) {
      setError(errMsg(err, "Restore failed."));
    } finally {
      setBusyId(null);
    }
  };

  const purge = async (u: DeletedUserRow) => {
    if (!confirm(`Permanently purge ${u.name}'s account? This cannot be undone. Their email/username will be freed up for reuse, but this account can no longer be restored afterward.`)) return;
    setBusyId(u.id);
    try {
      await usersApi.purge(u.id);
      refresh();
    } catch (err) {
      setError(errMsg(err, "Purge failed."));
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-2 flex-wrap">
        <div className="relative max-w-xs flex-1">
          <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-text-faint" />
          <input value={search} onChange={(e) => { setOffset(0); setSearch(e.target.value); }} placeholder="Search deleted accounts…" className="w-full bg-surface border border-border-soft rounded-[3px] pl-7 pr-3 py-2 text-[12.5px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none" />
        </div>
        <RowsPerPageSelect value={perPage} onChange={handlePerPageChange} />
      </div>

      {error && <div className="bg-rust/10 border border-rust/30 text-rust-soft text-[12px] rounded-[3px] px-3 py-2.5">{error}</div>}

      <div className="border border-border-soft bg-surface rounded-[3px] overflow-hidden">
        <table className="w-full text-left text-[12.5px]">
          <thead className="bg-surface-raised text-text-faint text-[11px] uppercase tracking-wide">
            <tr>
              <th className="px-5 py-3 font-medium">Name</th>
              <th className="hidden sm:table-cell px-5 py-3 font-medium">Role</th>
              <th className="hidden sm:table-cell px-5 py-3 font-medium">Deleted on</th>
              <th className="px-5 py-3 font-medium text-right">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border-soft">
            {loading && <tr><td colSpan={4} className="px-5 py-6 text-center text-text-faint">Loading…</td></tr>}
            {!loading && rows.length === 0 && <tr><td colSpan={4} className="px-5 py-8 text-center text-text-faint">No deleted accounts.</td></tr>}
            {rows.map((u) => (
              <tr key={u.id}>
                <td className="px-5 py-3">
                  <p className="text-text font-medium">{u.name}</p>
                  <p className="text-[11px] text-text-faint">{u.email}</p>
                </td>
                <td className="hidden sm:table-cell px-5 py-3 text-text-muted capitalize">{u.role.replace("_", " ")}</td>
                <td className="hidden sm:table-cell px-5 py-3 text-text-muted">{u.deleted_at ? formatWhen(u.deleted_at) : "—"}</td>
                <td className="px-5 py-3 text-right">
                  <div className="flex items-center justify-end gap-1.5 flex-wrap">
                    <button onClick={() => restore(u)} disabled={busyId === u.id} className="rounded-md border border-moss/40 px-2 py-1 text-[11px] font-medium text-moss-soft hover:bg-moss/10 disabled:opacity-50 transition-colors">Restore</button>
                    <button onClick={() => purge(u)} disabled={busyId === u.id} className="rounded-md border border-rust/40 px-2 py-1 text-[11px] font-medium text-rust-soft hover:bg-rust/10 disabled:opacity-50 transition-colors">Purge</button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <PaginationBar total={total} perPage={perPage} offset={offset} onOffsetChange={setOffset} />
    </div>
  );
}

// =============================================================================
// Restore Deleted Assets -- ported from js/components/assets.js's
// loadDeletedAssets()/restoreAssetPool()/purgeAssetPool(). require_super_admin
// on GET /assets/deleted, POST /assets/{id}/restore, POST /assets/{id}/purge
// -- Super Admin AND a plain Admin account, same as the main Asset Inventory
// table's manage actions; still not shown to Manager.
// =============================================================================

function DeletedAssetsPanel() {
  const [rows, setRows] = useState<DeletedAssetRow[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [perPage, setPerPage] = useState(DEFAULT_PAGE_SIZE);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = () => {
    setLoading(true);
    setError(null);
    assetsApi
      .listDeleted(perPage, offset, search)
      .then((res) => {
        setRows(res.items);
        setTotal(res.total);
      })
      .catch((err) => setError(errMsg(err, "Couldn't load deleted asset pools.")))
      .finally(() => setLoading(false));
  };

  useEffect(refresh, [offset, perPage, search]);

  const handlePerPageChange = (n: number) => {
    setPerPage(n);
    setOffset(0);
  };

  const restore = async (a: DeletedAssetRow) => {
    if (!confirm(`Restore asset pool "${a.name}"? It will reappear in the active Asset Inventory table immediately.`)) return;
    setBusyId(a.id);
    try {
      await assetsApi.restore(a.id);
      refresh();
    } catch (err) {
      setError(errMsg(err, "Restore failed."));
    } finally {
      setBusyId(null);
    }
  };

  const purge = async (a: DeletedAssetRow) => {
    if (!confirm(`Permanently purge asset pool "${a.name}"? This cannot be undone. Its name will be freed up for reuse by a new pool, but this pool can no longer be restored afterward.`)) return;
    setBusyId(a.id);
    try {
      await assetsApi.purge(a.id);
      refresh();
    } catch (err) {
      setError(errMsg(err, "Purge failed."));
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-2 flex-wrap">
        <div className="relative max-w-xs flex-1">
          <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-text-faint" />
          <input value={search} onChange={(e) => { setOffset(0); setSearch(e.target.value); }} placeholder="Search deleted pools…" className="w-full bg-surface border border-border-soft rounded-[3px] pl-7 pr-3 py-2 text-[12.5px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none" />
        </div>
        <RowsPerPageSelect value={perPage} onChange={handlePerPageChange} />
      </div>

      {error && <div className="bg-rust/10 border border-rust/30 text-rust-soft text-[12px] rounded-[3px] px-3 py-2.5">{error}</div>}

      <div className="border border-border-soft bg-surface rounded-[3px] overflow-hidden">
        <table className="w-full text-left text-[12.5px]">
          <thead className="bg-surface-raised text-text-faint text-[11px] uppercase tracking-wide">
            <tr>
              <th className="px-5 py-3 font-medium">Name</th>
              <th className="hidden sm:table-cell px-5 py-3 font-medium">Total qty</th>
              <th className="hidden sm:table-cell px-5 py-3 font-medium">Deleted on</th>
              <th className="px-5 py-3 font-medium text-right">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border-soft">
            {loading && <tr><td colSpan={4} className="px-5 py-6 text-center text-text-faint">Loading…</td></tr>}
            {!loading && rows.length === 0 && <tr><td colSpan={4} className="px-5 py-8 text-center text-text-faint">No deleted asset pools.</td></tr>}
            {rows.map((a) => (
              <tr key={a.id}>
                <td className="px-5 py-3">
                  <p className="text-text font-medium">{a.name}</p>
                  <p className="text-[11px] text-text-faint font-mono">POOL-{String(a.id).padStart(4, "0")}{a.category ? ` · ${a.category}` : ""}</p>
                </td>
                <td className="hidden sm:table-cell px-5 py-3 font-mono text-text-muted">{a.total_quantity}</td>
                <td className="hidden sm:table-cell px-5 py-3 text-text-muted">{a.deleted_at ? formatWhen(a.deleted_at) : "—"}</td>
                <td className="px-5 py-3 text-right">
                  <div className="flex items-center justify-end gap-1.5 flex-wrap">
                    <button onClick={() => restore(a)} disabled={busyId === a.id} className="rounded-md border border-moss/40 px-2 py-1 text-[11px] font-medium text-moss-soft hover:bg-moss/10 disabled:opacity-50 transition-colors">Restore</button>
                    <button onClick={() => purge(a)} disabled={busyId === a.id} className="rounded-md border border-rust/40 px-2 py-1 text-[11px] font-medium text-rust-soft hover:bg-rust/10 disabled:opacity-50 transition-colors">Purge</button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <PaginationBar total={total} perPage={perPage} offset={offset} onOffsetChange={setOffset} />
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
  const [perPage, setPerPage] = useState(DEFAULT_PAGE_SIZE);
  const [loading, setLoading] = useState(true);
  const [exporting, setExporting] = useState<"csv" | "pdf" | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true);
    auditApi.list(perPage, offset).then((res) => {
      setRows(res.items);
      setTotal(res.total);
      setLoading(false);
    });
  }, [offset, perPage]);

  const handlePerPageChange = (n: number) => {
    setPerPage(n);
    setOffset(0);
  };

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
      <div className="flex items-center gap-2 flex-wrap">
        <p className="text-[12.5px] text-text-muted">{total} entries in the ledger</p>
        <div className="ml-auto flex items-center gap-2">
          <RowsPerPageSelect value={perPage} onChange={handlePerPageChange} />
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

      <PaginationBar total={total} perPage={perPage} offset={offset} onOffsetChange={setOffset} />
    </div>
  );
}

// =============================================================================
// Quotes -- Admin/Manager view of the self-service Quotation feature,
// ported from the legacy frontend's js/components/quotation.js
// (initQuotesTab()/loadQuotes()/renderQuotesTable()). Same server-side
// search + pagination pattern as UsersPanel/OutsidersPanel above.
// require_privileged_role on the backend (Super Admin/Admin/Manager) --
// same `canDirectory` gate Admin() already uses for those two panels.
// =============================================================================

function statusBadgeClasses(status: string): string {
  if (status === "approved") return "bg-moss/15 text-moss-soft";
  if (status === "fulfilled") return "bg-sky/15 text-sky";
  if (status === "submitted") return "bg-brass/15 text-brass-soft";
  return "bg-surface-raised text-text-faint";
}

function statusLabel(status: string): string {
  if (status === "submitted") return "Pending Review";
  if (status === "approved") return "Approved";
  if (status === "fulfilled") return "Fulfilled";
  return status;
}

// One allocation row inside a shortfall line's "split across another
// outsourcing company" list -- mirrors the legacy frontend's
// shortfallAllocationRowHtml()/#shortfallRowsContainer-* (js/components/
// quotation.js). Quantity is kept as the source of truth for how much of
// the line's shortfall this row covers; sourced_from/unit_price are both
// optional (blank unit_price falls back to the depleted AssetType's own
// catalog price server-side).
interface ShortfallAllocRow { quantity: string; sourcedFrom: string; unitPrice: string }
interface ShortfallItemState { enabled: boolean; rows: ShortfallAllocRow[] }

function shortfallQtyFor(li: QuotationLineItem): number {
  return li.shortfall_quantity != null ? li.shortfall_quantity : li.quantity - (li.available_quantity ?? 0);
}

function FulfillmentPanel({ onCheckedOut }: { onCheckedOut: () => void }) {
  const [rows, setRows] = useState<FulfillmentQueueRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [checkingOutId, setCheckingOutId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Keyed by `${quoteId}:${itemId}` so state naturally clears itself once
  // refresh() pulls a fresh queue (a checked-out/no-longer-approved quote
  // just stops appearing, no separate cleanup needed).
  const [shortfall, setShortfall] = useState<Record<string, ShortfallItemState>>({});

  const refresh = () => {
    setLoading(true);
    quotationsApi.fulfillmentQueue().then((items) => {
      setError(null);
      setRows(items);
      setShortfall({});
      setLoading(false);
    }).catch((err) => {
      setError(errMsg(err, "Couldn't load the fulfillment queue."));
      setLoading(false);
    });
  };

  useEffect(refresh, []);

  const keyFor = (quoteId: number, itemId: number) => `${quoteId}:${itemId}`;

  const toggleShortfall = (quoteId: number, li: QuotationLineItem) => {
    if (li.item_id == null) return;
    const key = keyFor(quoteId, li.item_id);
    setShortfall((prev) => {
      const existing = prev[key];
      if (existing) return { ...prev, [key]: { ...existing, enabled: !existing.enabled } };
      // First time this line's checked -- pre-fill the single starting row
      // with the FULL shortfall quantity, so "just outsource it all to one
      // company" needs zero extra clicks.
      const qty = shortfallQtyFor(li);
      return { ...prev, [key]: { enabled: true, rows: [{ quantity: String(qty), sourcedFrom: "", unitPrice: "" }] } };
    });
  };

  const addAllocRow = (quoteId: number, li: QuotationLineItem) => {
    if (li.item_id == null) return;
    const key = keyFor(quoteId, li.item_id);
    const shortfallQty = shortfallQtyFor(li);
    setShortfall((prev) => {
      const existing = prev[key];
      if (!existing) return prev;
      // New row defaults to whatever's still unallocated, so splitting a
      // shortfall is "add a row" then adjust one number, not two.
      const allocated = existing.rows.reduce((sum, r) => sum + (parseInt(r.quantity, 10) || 0), 0);
      const remaining = Math.max(shortfallQty - allocated, 0);
      return { ...prev, [key]: { ...existing, rows: [...existing.rows, { quantity: remaining ? String(remaining) : "", sourcedFrom: "", unitPrice: "" }] } };
    });
  };

  const removeAllocRow = (quoteId: number, itemId: number, index: number) => {
    const key = keyFor(quoteId, itemId);
    setShortfall((prev) => {
      const existing = prev[key];
      if (!existing || existing.rows.length <= 1) return prev; // a lone row can't be removed
      return { ...prev, [key]: { ...existing, rows: existing.rows.filter((_, i) => i !== index) } };
    });
  };

  const updateAllocRow = (quoteId: number, itemId: number, index: number, patch: Partial<ShortfallAllocRow>) => {
    const key = keyFor(quoteId, itemId);
    setShortfall((prev) => {
      const existing = prev[key];
      if (!existing) return prev;
      const rows = existing.rows.map((r, i) => (i === index ? { ...r, ...patch } : r));
      return { ...prev, [key]: { ...existing, rows } };
    });
  };

  const checkout = async (row: FulfillmentQueueRow) => {
    setCheckingOutId(row.id);
    setError(null);
    try {
      const outsourceShortfallItems: QuotationOutsourceShortfallItem[] = row.items
        .filter((li) => li.stock_shortfall && !li.is_outsourced && li.item_id != null)
        .map((li) => ({ itemId: li.item_id as number, state: shortfall[keyFor(row.id, li.item_id as number)] }))
        .filter((x) => x.state?.enabled)
        .map(({ itemId, state }) => ({
          quotation_item_id: itemId,
          allocations: state!.rows
            .filter((r) => (parseInt(r.quantity, 10) || 0) > 0)
            .map((r) => ({
              quantity: parseInt(r.quantity, 10),
              sourced_from: r.sourcedFrom.trim() || null,
              unit_price: r.unitPrice.trim() === "" ? null : Number(r.unitPrice),
            })),
        }))
        .filter((item) => item.allocations.length > 0);

      await quotationsApi.checkout(row.id, outsourceShortfallItems);
      refresh();
      onCheckedOut();
    } catch (err) {
      setError(errMsg(err, `Couldn't check out ${row.reference_number}.`));
    } finally {
      setCheckingOutId(null);
    }
  };

  return (
    <div className="border border-border-soft bg-surface rounded-[3px] p-4">
      <div className="flex items-center justify-between mb-3">
        <h3 className="font-display text-[14px] font-medium text-text flex items-center gap-1.5"><PackageCheck size={14} /> Fulfillment queue</h3>
        <span className="text-[11px] text-text-faint">{rows.length} approved</span>
      </div>
      {error && <div className="bg-rust/10 border border-rust/30 text-rust-soft text-[12px] rounded-[3px] px-3 py-2.5 mb-3">{error}</div>}
      {loading && <p className="text-[12px] text-text-faint text-center py-6">Loading…</p>}
      {!loading && rows.length === 0 && <p className="text-[12px] text-text-faint text-center py-6">Nothing is Approved / Ready for Pickup right now.</p>}
      <div className="flex flex-col gap-2">
        {rows.map((q) => {
          // One control block per line that's both inventory-backed (an
          // already-outsourced line never had stock to begin with) AND
          // currently short. Checking its box tells POST /checkout to
          // source ONLY the shortfall externally -- whatever stock IS on
          // hand still checks out of inventory normally -- instead of
          // blocking the whole quote's checkout. Left unchecked, a
          // genuinely short line still blocks checkout exactly like before
          // this feature existed.
          const shortfallLines = q.items.filter((li) => li.stock_shortfall && !li.is_outsourced && li.item_id != null);
          return (
            <div key={q.id} className="border border-border-soft rounded-[3px] p-3">
              <div className="flex items-center justify-between gap-3">
                <div className="min-w-0">
                  <p className="font-mono text-[12.5px] text-text font-medium">{q.reference_number}</p>
                  <p className="text-[11px] text-text-faint truncate">
                    For {q.checkout_to ? `${q.checkout_to.name} (${q.checkout_to.email})` : "Unknown"} · {q.item_count} item(s) · approved {formatDate(q.approved_at)}
                  </p>
                  {q.has_shortfall && <p className="text-[11px] text-rust-soft mt-0.5">⚠ Not enough stock on hand for one or more lines below.</p>}
                </div>
                <div className="shrink-0 flex items-center gap-2">
                  <span className="font-mono text-[12.5px] text-text-muted">{formatPrice(q.total)}</span>
                  <button
                    onClick={() => checkout(q)}
                    disabled={checkingOutId === q.id}
                    className="flex items-center gap-1.5 bg-moss/15 hover:bg-moss/25 disabled:opacity-60 text-moss-soft text-[11.5px] font-medium rounded-[3px] px-2.5 py-1.5 transition-colors"
                  >
                    {checkingOutId === q.id ? <Loader2 size={11} className="animate-spin" /> : <CheckCircle size={11} />}
                    {checkingOutId === q.id ? "Checking out…" : "Check out"}
                  </button>
                </div>
              </div>

              {shortfallLines.map((li) => {
                const itemId = li.item_id as number;
                const key = keyFor(q.id, itemId);
                const shortfallQty = shortfallQtyFor(li);
                const state = shortfall[key];
                return (
                  <div key={key} className="mt-2 rounded-[3px] border border-rust/30 bg-rust/5 p-2.5">
                    <p className="text-[11px] font-medium text-rust-soft">
                      ⚠ '{li.asset_name}' needs {li.quantity}, only {li.available_quantity} available — {shortfallQty} short.
                    </p>
                    <label className="mt-1.5 flex cursor-pointer items-center gap-1.5 text-[12px] text-text-muted">
                      <input type="checkbox" checked={!!state?.enabled} onChange={() => toggleShortfall(q.id, li)} className="h-3.5 w-3.5 rounded border-border-soft" />
                      Source the {shortfallQty} short externally so {li.available_quantity} still checks out of stock
                    </label>
                    {state?.enabled && (
                      <div className="mt-1.5">
                        <div className="flex flex-col gap-1.5">
                          {state.rows.map((row, idx) => (
                            <div key={idx} className="grid grid-cols-[3.5rem_1fr_1fr_auto] items-center gap-1.5">
                              <input
                                type="number"
                                min={1}
                                value={row.quantity}
                                onChange={(e) => updateAllocRow(q.id, itemId, idx, { quantity: e.target.value })}
                                placeholder="Qty"
                                className="w-full rounded-[3px] border border-border-soft bg-ink-soft px-2 py-1 text-[12px] text-text focus:border-brass/50 focus:outline-none"
                              />
                              <input
                                type="text"
                                value={row.sourcedFrom}
                                onChange={(e) => updateAllocRow(q.id, itemId, idx, { sourcedFrom: e.target.value })}
                                placeholder="Sourced from (optional)"
                                className="w-full rounded-[3px] border border-border-soft bg-ink-soft px-2 py-1 text-[12px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none"
                              />
                              <input
                                type="number"
                                min={0}
                                step={0.01}
                                value={row.unitPrice}
                                onChange={(e) => updateAllocRow(q.id, itemId, idx, { unitPrice: e.target.value })}
                                placeholder={`Price/day (default ${formatPrice(li.unit_price ?? 0)})`}
                                className="w-full rounded-[3px] border border-border-soft bg-ink-soft px-2 py-1 text-[12px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none"
                              />
                              <button
                                type="button"
                                onClick={() => removeAllocRow(q.id, itemId, idx)}
                                title="Remove this source"
                                className={`rounded p-1 text-text-faint transition-colors hover:text-rust-soft ${state.rows.length <= 1 ? "invisible" : ""}`}
                              >
                                <X size={13} />
                              </button>
                            </div>
                          ))}
                        </div>
                        <button
                          type="button"
                          onClick={() => addAllocRow(q.id, li)}
                          className="mt-1.5 text-[11px] font-medium text-brass-soft hover:underline"
                        >
                          + Split across another outsourcing company
                        </button>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function QuotesPanel() {
  const [rows, setRows] = useState<QuotationListRow[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [perPage, setPerPage] = useState(DEFAULT_PAGE_SIZE);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [catalog, setCatalog] = useState<CatalogAsset[]>([]);
  const [openQuoteId, setOpenQuoteId] = useState<number | null>(null);
  const [approvingId, setApprovingId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [fulfillmentTick, setFulfillmentTick] = useState(0);

  const refresh = () => {
    setLoading(true);
    quotationsApi.list(perPage, offset, search).then((res) => {
      setError(null);
      setRows(res.items);
      setTotal(res.total);
      setLoading(false);
    }).catch((err) => {
      setError(errMsg(err, "Couldn't load quotations."));
      setLoading(false);
    });
  };

  useEffect(refresh, [offset, perPage, search]);
  useEffect(() => {
    quotationsApi.catalog().then(setCatalog).catch((err) => console.error("Failed to load catalog:", err));
  }, []);

  const handlePerPageChange = (n: number) => {
    setPerPage(n);
    setOffset(0);
  };

  const approve = async (q: QuotationListRow) => {
    setApprovingId(q.id);
    setError(null);
    try {
      await quotationsApi.approve(q.id);
      refresh();
      setFulfillmentTick((t) => t + 1);
    } catch (err) {
      setError(errMsg(err, `Couldn't approve ${q.reference_number}.`));
    } finally {
      setApprovingId(null);
    }
  };

  return (
    <div className="flex flex-col gap-4">
      {error && <div className="bg-rust/10 border border-rust/30 text-rust-soft text-[12px] rounded-[3px] px-3 py-2.5">{error}</div>}

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="lg:col-span-2 flex flex-col gap-4">
          <div className="flex items-center gap-2 flex-wrap">
            <div className="relative max-w-xs flex-1">
              <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-text-faint" />
              <input
                value={search}
                onChange={(e) => { setOffset(0); setSearch(e.target.value); }}
                placeholder="Search quotes…"
                className="w-full bg-surface border border-border-soft rounded-[3px] pl-7 pr-3 py-2 text-[12.5px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none"
              />
            </div>
            <RowsPerPageSelect value={perPage} onChange={handlePerPageChange} />
          </div>

          <div className="border border-border-soft bg-surface rounded-[3px] overflow-hidden">
            <table className="w-full text-left text-[12.5px]">
              <thead className="bg-surface-raised text-text-faint text-[11px] uppercase tracking-wide">
                <tr>
                  <th className="px-5 py-3 font-medium">Reference</th>
                  <th className="hidden sm:table-cell px-5 py-3 font-medium">Requester</th>
                  <th className="hidden sm:table-cell px-5 py-3 font-medium">Status</th>
                  <th className="px-5 py-3 font-medium text-right">Total</th>
                  <th className="px-5 py-3 font-medium text-right">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border-soft">
                {loading && <tr><td colSpan={5} className="px-5 py-6 text-center text-text-faint">Loading…</td></tr>}
                {!loading && rows.length === 0 && <tr><td colSpan={5} className="px-5 py-8 text-center text-text-faint">No submitted quotes yet.</td></tr>}
                {rows.map((q) => (
                  <tr key={q.id}>
                    <td className="px-5 py-3">
                      <p className="font-mono text-text font-medium">{q.reference_number}</p>
                      <p className="text-[11px] text-text-faint sm:hidden">{formatDate(q.submitted_at)} · {q.item_count} item(s)</p>
                    </td>
                    <td className="hidden sm:table-cell px-5 py-3">
                      <p className="text-text-muted">{q.requester ? q.requester.name : "—"}</p>
                      <p className="text-[11px] text-text-faint">{q.requester?.email ?? ""}</p>
                    </td>
                    <td className="hidden sm:table-cell px-5 py-3">
                      <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium ${statusBadgeClasses(q.status)}`}>{statusLabel(q.status)}</span>
                    </td>
                    <td className="px-5 py-3 text-right font-mono text-text-muted">{formatPrice(q.total)}</td>
                    <td className="px-5 py-3 text-right">
                      <div className="flex items-center justify-end gap-1.5 flex-wrap">
                        {q.status === "submitted" && (
                          <button
                            onClick={() => approve(q)}
                            disabled={approvingId === q.id}
                            className="rounded-md border border-border-soft px-2 py-1 text-[11px] font-medium text-text-muted hover:border-moss/50 hover:text-moss-soft transition-colors disabled:opacity-50"
                          >
                            Approve
                          </button>
                        )}
                        <button onClick={() => setOpenQuoteId(q.id)} className="rounded-md border border-border-soft px-2 py-1 text-[11px] font-medium text-text-muted hover:border-brass/50 hover:text-brass-soft transition-colors">
                          {q.locked ? "View" : "View / Adjust"}
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <PaginationBar total={total} perPage={perPage} offset={offset} onOffsetChange={setOffset} />
        </div>

        <FulfillmentPanel key={fulfillmentTick} onCheckedOut={refresh} />
      </div>

      <QuoteDetailDrawer
        mode="admin"
        quotationId={openQuoteId}
        catalog={catalog}
        onClose={() => setOpenQuoteId(null)}
        onChanged={refresh}
      />
    </div>
  );
}

// =============================================================================
// Admin / Manager -- two separate pages/routes (/admin, /manager) sharing
// one implementation, mirroring the legacy frontend's admin.html vs
// manager.html: same underlying panels, but their own URL, header, and
// mode pill rather than one page whose contents silently reshape around
// whoever happens to be signed in. Which route a role can even reach is
// enforced up in App.tsx's RequireRole; the tab visibility below is the
// second, finer-grained layer -- see lib/roles.ts.
// =============================================================================

export function Admin() {
  return <AdminOrManagerPage variant="admin" />;
}

export function Manager() {
  return <AdminOrManagerPage variant="manager" />;
}

function AdminOrManagerPage({ variant }: { variant: "admin" | "manager" }) {
  const { user, demo } = useAuth();
  const isManager = variant === "manager";
  const [searchParams, setSearchParams] = useSearchParams();

  // ?custody=user:5&name=Jane -- deep link from the Notification Bell
  // (see components/NotificationBell.tsx's openCustody()). Parsed once per
  // navigation; the query string is cleared right after the target panel
  // consumes it so switching tabs or refreshing doesn't reopen the drawer.
  const custodyParam = searchParams.get("custody");
  const custodyName = searchParams.get("name") ?? "";
  const [custodyType, custodyIdRaw] = custodyParam ? custodyParam.split(":") : [null, null];
  const custodyId = custodyIdRaw ? Number(custodyIdRaw) : null;
  const deepLinkTarget = custodyId != null && !Number.isNaN(custodyId) ? { id: custodyId, name: custodyName } : null;
  const clearCustodyParam = () => setSearchParams((prev) => { const next = new URLSearchParams(prev); next.delete("custody"); next.delete("name"); return next; }, { replace: true });
  // The Manager page never offers Inventory Import or System Backups --
  // same as manager.html never having those sections at all, regardless
  // of who's actually viewing it -- while the Admin page keeps gating
  // them on the visitor's real role exactly as before.
  const canImport = !isManager && (demo || isFullAdmin(user?.role));
  // Backups stay Super-Admin-only (deps.require_true_super_admin) -- a
  // backup contains literally everything, including every `admin`
  // account's own credentials, so letting an `admin` view/restore one
  // would let that action expose or tamper with the very accounts meant
  // to be holding it accountable.
  const canBackups = !isManager && (demo || isTrueSuperAdmin(user?.role));
  const canDirectory = demo || isPrivileged(user?.role);
  // reset-password/delete/restore/purge (deps.require_super_admin) treat
  // Admin and Super Admin identically on the backend -- mirrored here as
  // isFullAdmin rather than isTrueSuperAdmin so a plain Admin account
  // actually gets those affordances the backend already grants it.
  const canManageAccounts = demo || isFullAdmin(user?.role);
  // Create/Edit/Revoke are also open to a Manager -- POST /users, PUT
  // /users/{id}, and POST /users/{id}/convert-to-outsider are all
  // require_privileged_role, not require_super_admin (only reset-
  // password/delete/restore/purge above are Super-Admin/Admin-only). A
  // Manager is further limited to staff/customer accounts specifically
  // (see lib/roles.ts's canManageUserRole()), enforced per-row inside
  // UsersPanel/OutsidersPanel rather than by hiding the whole tab.
  const canCreateAccounts = demo || isFullAdmin(user?.role) || user?.role === "manager";
  // Same require_super_admin gate as GET /assets/deleted and
  // POST /assets/{id}/restore -- Admin, not just the root Super Admin.
  const canDeletedAssets = !isManager && (demo || isFullAdmin(user?.role));
  // PUT /settings/vat is require_super_admin too -- kept as its own flag
  // (rather than reusing canBackups) since it's a different backend gate
  // that just happens to share a tab group with System Backups.
  const canSettings = !isManager && (demo || isFullAdmin(user?.role));

  type Tab = { key: "import" | "backups" | "users" | "outsiders" | "audit" | "quotes" | "deleted-assets" | "deleted-users" | "settings"; label: string; icon: typeof FileSpreadsheet };
  const tabs = useMemo<Tab[]>(() => {
    const list: Tab[] = [];
    if (canDirectory) list.push({ key: "users", label: "User Directory", icon: UsersIcon });
    if (canDirectory) list.push({ key: "outsiders", label: "Ad-Hoc Directory", icon: Contact });
    if (canDirectory) list.push({ key: "quotes", label: "Quotes", icon: FileText });
    if (canDirectory) list.push({ key: "audit", label: "Audit Trail", icon: ScrollText });
    if (canImport) list.push({ key: "import", label: "Inventory Import", icon: FileSpreadsheet });
    if (canDeletedAssets) list.push({ key: "deleted-assets", label: "Deleted Assets", icon: Boxes });
    if (canDeletedAssets) list.push({ key: "deleted-users", label: "Deleted Users", icon: UserMinus });
    if (canBackups) list.push({ key: "backups", label: "System Backups", icon: DatabaseBackup });
    if (canSettings) list.push({ key: "settings", label: "Settings", icon: Percent });
    return list;
  }, [canImport, canBackups, canDirectory, canDeletedAssets, canSettings]);

  const [tab, setTab] = useState<Tab["key"] | null>(tabs[0]?.key ?? null);
  useEffect(() => {
    if (!tabs.some((t) => t.key === tab)) setTab(tabs[0]?.key ?? null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tabs]);
  // A deep link from the Notification Bell forces the matching directory
  // tab open, regardless of whatever tab was showing before.
  useEffect(() => {
    if (custodyType === "outsider") setTab("outsiders");
    else if (custodyType === "user") setTab("users");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [custodyType, custodyId]);

  return (
    <div>
      <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }} className="mb-6 flex items-start justify-between gap-3 flex-wrap">
        <div>
          <h1 className="font-display text-2xl font-semibold text-text">{isManager ? "Manager" : "Admin"}</h1>
          <p className="text-text-muted text-sm mt-1">
            {isManager
              ? "Directories, quotes, and the audit trail — scoped to what a Manager can see and do."
              : "Directories, audit trail, inventory import, and system-level backup controls."}
          </p>
        </div>
        <span
          className={`hidden sm:flex items-center gap-1.5 rounded-full border px-3 py-1 text-[11px] font-semibold uppercase tracking-wide ${
            isManager ? "border-brass/50 bg-brass/10 text-brass-soft" : "border-rust/50 bg-rust/10 text-rust-soft"
          }`}
        >
          <span className={`h-1.5 w-1.5 rounded-full ${isManager ? "bg-brass" : "bg-rust"} animate-pulse`} />
          {isManager ? "Manager Mode" : "Admin Mode"}
        </span>
      </motion.div>

      {tabs.length === 0 ? (
        <div className="flex flex-col items-center justify-center text-center py-20 border border-border-soft rounded-[3px] bg-surface">
          <ShieldAlert size={20} className="text-text-faint mb-3" />
          <p className="text-[13px] text-text-muted max-w-sm">
            Your role doesn't include access to anything in {isManager ? "Manager" : "Admin"}. Ask a Super Admin if you need something here.
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

          {tab === "users" && canDirectory && (
            <UsersPanel
              canManage={canManageAccounts}
              canCreate={canCreateAccounts}
              actorRole={user?.role}
              demo={demo}
              openCustody={custodyType === "user" ? deepLinkTarget : null}
              onOpenedCustody={clearCustodyParam}
            />
          )}
          {tab === "outsiders" && canDirectory && (
            <OutsidersPanel
              canManage={canDirectory}
              actorRole={user?.role}
              demo={demo}
              openCustody={custodyType === "outsider" ? deepLinkTarget : null}
              onOpenedCustody={clearCustodyParam}
            />
          )}
          {tab === "quotes" && canDirectory && <QuotesPanel />}
          {tab === "audit" && canDirectory && <AuditPanel />}
          {tab === "import" && canImport && <InventoryImportPanel />}
          {tab === "deleted-assets" && canDeletedAssets && <DeletedAssetsPanel />}
          {tab === "deleted-users" && canDeletedAssets && <DeletedUsersPanel />}
          {tab === "backups" && canBackups && <SystemBackupsPanel />}
          {tab === "settings" && canSettings && <SettingsPanel />}
        </>
      )}
    </div>
  );
}
