import { Download } from "lucide-react";

/**
 * Shared "download a file from a signed GET endpoint" trigger, reused
 * across every properties-assigned/directory/quotation export button in
 * the app (My Items, the Custody Ledger drawer, the User/Ad-Hoc
 * Directories, and the Quotation cart/detail exports). Mirrors the
 * legacy frontend's components/exports.js -- one small helper
 * (downloadExport()) behind every one of those buttons -- just as a
 * plain-anchor click instead of a blob fetch, the same direct-download
 * approach AssetExportModal.tsx already uses for the Asset Inventory
 * export.
 *
 * Pass a single "pdf" format for the Quotation exports (the backend
 * always returns a PDF document there, no `format` query param), or
 * both "csv" and "pdf" for the tabular directory/custody exports.
 */
export function ExportButtons({
  formats = ["csv", "pdf"],
  urlFor,
  filenameFor,
  disabled = false,
  compact = false,
}: {
  formats?: ("csv" | "pdf")[];
  urlFor: (format: "csv" | "pdf") => string;
  filenameFor: (format: "csv" | "pdf") => string;
  disabled?: boolean;
  /** Icon-only buttons for tight spaces (e.g. inside a drawer header). */
  compact?: boolean;
}) {
  const download = (format: "csv" | "pdf") => {
    if (disabled) return;
    const a = document.createElement("a");
    a.href = urlFor(format);
    a.download = filenameFor(format);
    a.click();
  };

  return (
    <div className="flex items-center gap-1.5">
      {formats.map((format) => (
        <button
          key={format}
          type="button"
          onClick={() => download(format)}
          disabled={disabled}
          title={`Export ${format.toUpperCase()}`}
          className="flex items-center gap-1 rounded-md border border-border-soft px-2 py-1 text-[11px] font-medium text-text-muted hover:border-brass/50 hover:text-brass-soft disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
        >
          <Download size={11} /> {compact ? format.toUpperCase() : `Export ${format.toUpperCase()}`}
        </button>
      ))}
    </div>
  );
}
