// =============================================================================
// Audit Trail -- ported from js/components/audit.js. Export runs async on a
// Celery worker: enqueue -> poll status every ~1.5s -> download when ready.
// =============================================================================
import { useEffect, useRef, useState } from "react";
import { X, Download, Loader2 } from "lucide-react";
import { auditApi } from "../../lib/api";
import type { AuditLogEntry } from "../../lib/types";
import { PaginationBar, RowsPerPageSelect } from "../../components/PaginationBar";
import { DEFAULT_PAGE_SIZE } from "../../lib/pagination";
import { useRequestGuard } from "../../lib/useRequestGuard";
import { Modal } from "../../components/ui/Modal";
import { ErrorBanner } from "../../components/ui/ErrorBanner";
import { TableShell, TableHead, TablePlaceholderRow } from "../../components/ui/TableShell";
import { errMsg } from "./sharedHelpers";

export function AuditPanel() {
  const [rows, setRows] = useState<AuditLogEntry[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [perPage, setPerPage] = useState(DEFAULT_PAGE_SIZE);
  const [loading, setLoading] = useState(true);
  const [exportModalOpen, setExportModalOpen] = useState(false);
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [exporting, setExporting] = useState<"csv" | "pdf" | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const beginListRequest = useRequestGuard();
  const mountedRef = useRef(true);

  useEffect(() => () => { mountedRef.current = false; }, []);

  useEffect(() => {
    const isCurrent = beginListRequest();
    setLoading(true);
    setLoadError(null);
    auditApi.list(perPage, offset).then((res) => {
      if (!isCurrent()) return;
      setRows(res.items);
      setTotal(res.total);
      setLoading(false);
    }).catch((err) => {
      if (!isCurrent()) return;
      console.error("Failed to load audit ledger:", err);
      setRows([]);
      setTotal(0);
      setLoadError(errMsg(err, "Audit Trail could not be loaded."));
      setLoading(false);
    });
  }, [offset, perPage, beginListRequest]);

  useEffect(() => {
    if (offset > 0 && offset >= total) setOffset(Math.max(0, Math.floor(Math.max(total - 1, 0) / perPage) * perPage));
  }, [offset, total, perPage]);

  const handlePerPageChange = (n: number) => {
    setPerPage(n);
    setOffset(0);
  };

  // Same date-range gate as legacy's #auditExportModal (js/components/
  // audit.js's openAuditExportModal()/exportAuditLogs()): "Export Ledger"
  // opens a From/To range picker -- both optional, defaulting to the full
  // (unbounded) ledger -- rather than immediately generating a file, since
  // the ledger has no natural size limit and a wide/unscoped export can
  // take a while to build on the worker.
  const openExportModal = () => {
    setStartDate("");
    setEndDate("");
    setExportError(null);
    setExportModalOpen(true);
  };

  const runExport = async (format: "csv" | "pdf") => {
    setExporting(format);
    setExportError(null);
    try {
      const { task_id } = await auditApi.startExport(format, startDate || undefined, endDate || undefined);
      setExportModalOpen(false);
      const deadline = Date.now() + 60_000;
      while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 1500));
        if (!mountedRef.current) return;
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
      if (mountedRef.current) setExportError(errMsg(err, "Export failed."));
    } finally {
      if (mountedRef.current) setExporting(null);
    }
  };

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-2 flex-wrap">
        <p className="text-[12.5px] text-text-muted">{total} entries in the ledger</p>
        <div className="ml-auto flex items-center gap-2">
          <RowsPerPageSelect value={perPage} onChange={handlePerPageChange} />
          <button onClick={openExportModal} disabled={!!exporting} className="flex items-center gap-1.5 border border-border-soft hover:border-brass/40 disabled:opacity-60 text-[12px] text-text rounded-[3px] px-3 py-1.5 transition-colors">
            {exporting ? <Loader2 size={12} className="animate-spin" /> : <Download size={12} />} Export Ledger
          </button>
        </div>
      </div>
      {loadError && <ErrorBanner>{loadError}</ErrorBanner>}
      {exportError && !exportModalOpen && <ErrorBanner>{exportError}</ErrorBanner>}

      <TableShell>
        <table className="w-full text-left text-[12.5px]">
          <TableHead>
            <th className="px-5 py-3 font-medium">Timestamp</th>
            <th className="px-5 py-3 font-medium">Operator</th>
            <th className="hidden sm:table-cell px-5 py-3 font-medium">Action</th>
            <th className="hidden sm:table-cell px-5 py-3 font-medium">Details</th>
          </TableHead>
          <tbody className="divide-y divide-border-soft">
            {loading && <TablePlaceholderRow columns={4}>Loading…</TablePlaceholderRow>}
            {!loading && rows.length === 0 && <TablePlaceholderRow columns={4}>No entries.</TablePlaceholderRow>}
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
      </TableShell>

      <PaginationBar total={total} perPage={perPage} offset={offset} onOffsetChange={setOffset} />

      {exportModalOpen && (
        <Modal onClose={() => !exporting && setExportModalOpen(false)}>
          <div className="flex items-start justify-between mb-4">
            <div>
              <p className="text-[11px] font-semibold uppercase tracking-wide text-text-faint">Audit Logs</p>
              <h2 className="font-display text-lg font-semibold text-text">Export Ledger</h2>
            </div>
            <button onClick={() => !exporting && setExportModalOpen(false)} className="text-text-faint hover:text-text transition-colors"><X size={16} /></button>
          </div>
          <div className="flex flex-col gap-3">
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="mb-1 block text-[12px] font-medium text-text-muted">From</label>
                <input type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} className="w-full bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2 text-[13px] text-text focus:border-brass/50 focus:outline-none" />
              </div>
              <div>
                <label className="mb-1 block text-[12px] font-medium text-text-muted">To</label>
                <input type="date" value={endDate} onChange={(e) => setEndDate(e.target.value)} className="w-full bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2 text-[13px] text-text focus:border-brass/50 focus:outline-none" />
              </div>
            </div>
            <p className="text-[11px] text-text-faint">Leave either field blank for no start/end limit. Exports the full ledger by default.</p>
            {exportError && <ErrorBanner>{exportError}</ErrorBanner>}
            <div className="flex gap-3 pt-1">
              <button onClick={() => runExport("csv")} disabled={!!exporting} className="flex-1 flex items-center justify-center gap-1.5 border border-border-soft hover:border-brass/40 disabled:opacity-60 text-[13px] font-semibold text-text rounded-[3px] py-2.5 transition-colors">
                {exporting === "csv" ? <Loader2 size={13} className="animate-spin" /> : <Download size={13} />} .CSV
              </button>
              <button onClick={() => runExport("pdf")} disabled={!!exporting} className="flex-1 flex items-center justify-center gap-1.5 bg-brass hover:bg-brass-soft disabled:opacity-60 text-[13px] font-semibold text-ink rounded-[3px] py-2.5 transition-colors">
                {exporting === "pdf" ? <Loader2 size={13} className="animate-spin" /> : <Download size={13} />} .PDF
              </button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}
