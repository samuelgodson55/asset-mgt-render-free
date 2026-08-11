// =============================================================================
// Inventory Import -- ported from the legacy frontend's
// js/components/assets.js (downloadCsvImportTemplate / submitCsvImportForm).
// Available to Super Admin AND a plain Admin account (require_super_admin
// on POST /assets/import -- the broader "full admin" gate).
// =============================================================================
import { useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { UploadCloud, Download, FileSpreadsheet, TriangleAlert, Loader2, CheckCircle2 } from "lucide-react";
import { importApi } from "../../lib/api";
import type { ImportResult } from "../../lib/types";
import { ErrorBanner } from "../../components/ui/ErrorBanner";
import { errMsg } from "./sharedHelpers";

const CSV_IMPORT_TEMPLATE_ROWS = [
  ["name", "total_quantity", "category", "department", "price"],
  ["Dell Latitude 5440", "10", "Engineering", "Camera", "899.00"],
  ["Logitech MX Master 3S", "25", "Engineering", "Grip", "99.99"],
  ["Herman Miller Aeron Chair", "8", "", "Production", "1395.00"],
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

export function InventoryImportPanel() {
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
              Upload a CSV of <span className="font-mono text-[11.5px]">name, total_quantity, category, department, price</span> rows. Matching pool names <span className="text-text">add</span> to the existing quantity rather than replacing it.
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
          <div className="mt-4">
            <ErrorBanner icon={<TriangleAlert size={13} />}>{error}</ErrorBanner>
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
            ["category", "Existing category. Preserved independently from department."],
            ["department", "Optional asset department, e.g. Camera, Lighting, Grip."],
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
