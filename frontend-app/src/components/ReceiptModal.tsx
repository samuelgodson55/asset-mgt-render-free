// ---------------------------------------------------------------------------
// components/ReceiptModal.tsx
// -----------------------------------------------------------------------------
// Scannable checkout receipt. Checkouts here are pool-based -- there's no
// per-unit serial tracked day-to-day -- so this isn't a lookup key into the
// backend, it's a self-contained snapshot of what's out and when it's due:
//   - QR code: encodes a plain-text summary (lib/receipt.ts's
//     buildReceiptScanText()) that ANY phone camera can read on its own,
//     no app/login/network round-trip needed. "Scan to see what you have
//     out and when it's due."
//   - Barcode (Code128): encodes a short reference (buildReceiptCode()) --
//     a handheld scanner or a scan-to-search box (see CustodyDrawer.tsx's
//     "Quick find") can key off it fast during in-person check-in.
// Opened from DispatchModal (right after issuing), Checkouts.tsx and
// MyItems.tsx (one row at a time), and CustodyDrawer.tsx (a person's whole
// ledger). All of them build a ReceiptTarget and hand it to this component --
// this file doesn't know or care which page it came from.
// ---------------------------------------------------------------------------
import { useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import QRCode from "qrcode";
import JsBarcode from "jsbarcode";
import { X, Printer, Copy, Check, PackageCheck } from "lucide-react";
import { buildReceiptCode, buildReceiptScanText, isDueSoon, isOverdue, safeDateLabel, type ReceiptTarget } from "../lib/receipt";

export function ReceiptModal({ target, onClose }: { target: ReceiptTarget | null; onClose: () => void }) {
  const [qrDataUrl, setQrDataUrl] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const barcodeRef = useRef<HTMLCanvasElement | null>(null);

  // Frozen the moment a receipt is opened -- re-rendering (e.g. a live
  // "due soon" recheck) shouldn't make the "Issued" timestamp drift.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const issuedAt = useMemo(() => new Date(), [target]);
  const scanText = useMemo(() => (target ? buildReceiptScanText(target) : ""), [target]);
  const code = useMemo(() => (target ? buildReceiptCode(target) : ""), [target]);

  useEffect(() => {
    setCopied(false);
    if (!target) {
      setQrDataUrl(null);
      return;
    }
    let cancelled = false;
    // Same "read the live theme tokens, not fixed hex" approach as
    // Login.tsx's MFA-setup QR -- the tile matches whichever theme
    // (dark/light) is active instead of always rendering dark-mode colors.
    const styles = getComputedStyle(document.documentElement);
    const dark = styles.getPropertyValue("--color-text").trim() || "#0F1219";
    const light = styles.getPropertyValue("--color-ink-soft").trim() || "#EFE7D4";
    QRCode.toDataURL(scanText, { margin: 1, width: 220, color: { dark, light } })
      .then((url) => { if (!cancelled) setQrDataUrl(url); })
      .catch(() => { if (!cancelled) setQrDataUrl(null); });
    return () => { cancelled = true; };
  }, [target, scanText]);

  useEffect(() => {
    if (!target || !barcodeRef.current) return;
    const styles = getComputedStyle(document.documentElement);
    const dark = styles.getPropertyValue("--color-text").trim() || "#0F1219";
    try {
      JsBarcode(barcodeRef.current, code, {
        format: "CODE128",
        lineColor: dark,
        background: "transparent",
        width: 1.6,
        height: 42,
        displayValue: true,
        fontSize: 11,
        margin: 4,
        font: "IBM Plex Mono, monospace",
      });
    } catch {
      // A code with characters JsBarcode can't encode is purely
      // cosmetic fallback -- the QR above still carries the full receipt.
    }
  }, [target, code]);

  if (!target) return null;

  const copyScanText = async () => {
    try {
      await navigator.clipboard.writeText(scanText);
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    } catch {
      // Clipboard permission denied or unavailable -- non-fatal, the
      // person can still read/print the receipt normally.
    }
  };

  return (
    <AnimatePresence>
      <motion.div key="backdrop" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onClick={onClose} className="receipt-no-print fixed inset-0 bg-ink/70 backdrop-blur-sm z-40" />
      <motion.div key="panel"
        initial={{ opacity: 0, y: 16, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        exit={{ opacity: 0, y: 8, scale: 0.98 }}
        transition={{ duration: 0.25, ease: [0.16, 1, 0.3, 1] }}
        className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-50 w-full max-w-sm max-h-[90vh] overflow-y-auto"
      >
        <div className="receipt-no-print flex items-center justify-between mb-2 px-0.5">
          <p className="text-[11px] uppercase tracking-wider text-text-faint">Checkout receipt</p>
          <button onClick={onClose} className="text-text-faint hover:text-text transition-colors"><X size={16} /></button>
        </div>

        {/* The printable ticket itself -- everything else in the app is
            hidden at print time via #receipt-print-area in index.css, so
            "Print" produces just this card (works as a real receipt or a
            Save-as-PDF from the browser's print dialog). */}
        <div id="receipt-print-area" className="tag-notch relative bg-surface border border-border-soft rounded-[4px] overflow-hidden">
          <div className="absolute left-4 top-1/2 -translate-y-1/2 w-3 h-3 rounded-full bg-ink border border-border-soft z-10 receipt-no-print" />
          <div className="px-6 pt-6 pb-5">
            <div className="flex items-start justify-between gap-3">
              <div>
                <p className="font-display text-[15px] font-semibold text-text">Checkout Receipt</p>
                <p className="text-[11px] text-text-faint font-mono mt-0.5">Issued {issuedAt.toLocaleString(undefined, { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" })}</p>
              </div>
              <PackageCheck size={18} className="text-moss-soft shrink-0 mt-0.5" />
            </div>

            <div className="mt-4 pt-4 border-t border-dashed border-border-soft">
              <p className="text-[10px] uppercase tracking-wider text-text-faint">Held by</p>
              <p className="text-[14px] text-text font-medium mt-0.5">{target.holderName}</p>
              {target.holderSubtitle && <p className="text-[11.5px] text-text-muted mt-0.5">{target.holderSubtitle}</p>}
              {target.note && <p className="text-[11px] text-text-faint italic mt-1">{target.note}</p>}
            </div>

            <div className="mt-4 pt-4 border-t border-dashed border-border-soft flex flex-col gap-3">
              {target.items.map((item, i) => {
                const overdue = isOverdue(item.due_date);
                const dueSoon = isDueSoon(item.due_date);
                return (
                  <div key={item.checkout_id ?? i} className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <p className="text-[12.5px] text-text truncate">{item.asset_name}</p>
                      <p className="text-[10.5px] text-text-faint font-mono mt-0.5">{item.tag ? `${item.tag} · ` : ""}qty {item.quantity}</p>
                    </div>
                    <div className="text-right shrink-0">
                      <p className="text-[11.5px] text-text font-mono">{safeDateLabel(item.due_date)}</p>
                      {overdue && <p className="text-[10px] font-medium text-rust-soft mt-0.5">Overdue</p>}
                      {!overdue && dueSoon && <p className="text-[10px] font-medium text-brass-soft mt-0.5">Due soon</p>}
                    </div>
                  </div>
                );
              })}
              {target.items.length === 0 && <p className="text-[12px] text-text-faint text-center py-2">Nothing currently on loan.</p>}
            </div>

            <div className="mt-5 pt-5 border-t border-dashed border-border-soft flex flex-col items-center gap-3">
              {qrDataUrl ? (
                <img src={qrDataUrl} alt="Scan to see what you have out and when it's due" width={160} height={160} className="rounded-[2px]" />
              ) : (
                <div className="w-[160px] h-[160px] rounded-[2px] bg-ink-soft animate-pulse" />
              )}
              <p className="text-[10.5px] text-text-faint text-center max-w-[220px]">Scan to see what's checked out and when it's due -- no login needed.</p>

              <canvas ref={barcodeRef} className="max-w-full mt-1" />
              <p className="text-[10px] text-text-faint text-center">For fast lookup at in-person check-in</p>
            </div>
          </div>
          <div className="perforation-h h-0 border-t border-dashed border-border-soft" />
          <div className="px-6 py-2.5 text-center text-[10px] text-text-faint tracking-wide">Asset Management System</div>
        </div>

        <div className="receipt-no-print flex items-center gap-2 mt-4">
          <button
            onClick={() => window.print()}
            className="flex-1 flex items-center justify-center gap-1.5 bg-brass hover:bg-brass-soft text-ink font-medium text-[13px] rounded-[3px] py-2.5 transition-colors"
          >
            <Printer size={13} /> Print
          </button>
          <button
            onClick={copyScanText}
            className="flex-1 flex items-center justify-center gap-1.5 border border-border-soft hover:border-brass/50 text-text-muted hover:text-brass-soft text-[13px] font-medium rounded-[3px] py-2.5 transition-colors"
          >
            {copied ? <Check size={13} className="text-moss-soft" /> : <Copy size={13} />} {copied ? "Copied" : "Copy scan text"}
          </button>
        </div>
      </motion.div>
    </AnimatePresence>
  );
}
