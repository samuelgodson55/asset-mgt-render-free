// ---------------------------------------------------------------------------
// Checkout receipts (QR + barcode) -- shared by ReceiptModal.tsx and every
// page that can hand someone a "here's what you have out" ticket:
// DispatchModal (right after issuing), Checkouts.tsx (admin/staff list, one
// row at a time), MyItems.tsx (self-service, one row or everything), and
// CustodyDrawer.tsx (a person's whole ledger, for in-person check-in).
//
// Checkouts here are pool-based -- there's no per-unit serial to scan, so
// the receipt isn't a lookup key into the backend. It's a small,
// self-contained snapshot: what's out, to whom, and when it's due. That's
// deliberate -- it means the QR code renders something readable by ANY
// phone camera (no login, no network round-trip), and the barcode gives
// staff something fast to key off during in-person check-in without
// depending on a new backend endpoint.
// ---------------------------------------------------------------------------

export interface ReceiptLineItem {
  checkout_id?: number | null;
  asset_name: string;
  tag?: string | null;
  quantity: number;
  due_date?: string | null;
  checked_out_at?: string | null;
}

export interface ReceiptTarget {
  /** Person the receipt is for -- "Jane Doe", "Acme Corp — Sam Lee", etc. */
  holderName: string;
  /** Small line under the holder name -- email, role, "Outsider", etc. */
  holderSubtitle?: string | null;
  /** Context line under the title -- "Issued at dispatch", "Custody ledger snapshot". */
  note?: string | null;
  items: ReceiptLineItem[];
}

function safeDateLabel(iso?: string | null): string {
  if (!iso) return "No due date";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "No due date";
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

export function isOverdue(dueDate?: string | null): boolean {
  if (!dueDate) return false;
  const d = new Date(dueDate);
  if (Number.isNaN(d.getTime())) return false;
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  return d.getTime() < today.getTime();
}

export function isDueSoon(dueDate?: string | null, withinDays = 3): boolean {
  if (!dueDate || isOverdue(dueDate)) return false;
  const d = new Date(dueDate);
  if (Number.isNaN(d.getTime())) return false;
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const diffDays = Math.round((d.getTime() - today.getTime()) / 86400000);
  return diffDays >= 0 && diffDays <= withinDays;
}

// QR codes get dense (and harder for a phone camera to lock onto) past a
// certain point, so a big custody ledger only gets its first several lines
// spelled out in the scan text -- the on-screen/printed card below still
// lists every item in full either way.
const MAX_SCAN_LINES = 10;

/**
 * Plain text encoded into the QR code. No app, login, or network access
 * needed to read it -- any phone's camera app shows this directly.
 */
export function buildReceiptScanText(target: ReceiptTarget): string {
  const lines: string[] = [];
  lines.push("CHECKOUT RECEIPT");
  lines.push(target.holderName);
  lines.push(`Issued ${safeDateLabel(new Date().toISOString())}`);
  lines.push("------------------------------");

  const shown = target.items.slice(0, MAX_SCAN_LINES);
  shown.forEach((item, i) => {
    const tagPart = item.tag ? ` [${item.tag}]` : "";
    lines.push(`${i + 1}. ${item.asset_name}${tagPart} x${item.quantity}`);
    const flag = isOverdue(item.due_date) ? " -- OVERDUE" : isDueSoon(item.due_date) ? " -- due soon" : "";
    lines.push(`   due ${safeDateLabel(item.due_date)}${flag}`);
  });
  if (target.items.length > shown.length) {
    lines.push(`...+${target.items.length - shown.length} more item(s) -- see full receipt`);
  }

  lines.push("------------------------------");
  lines.push(`${target.items.length} item(s) on loan · Asset Management`);
  return lines.join("\n");
}

/**
 * Short value encoded into the Code128 barcode -- fast for a handheld
 * scanner (or a scan-to-search box, see CustodyDrawer.tsx) to key off
 * during in-person check-in, without needing the full QR payload.
 */
export function buildReceiptCode(target: ReceiptTarget): string {
  const ids = target.items.map((i) => i.checkout_id).filter((id): id is number => typeof id === "number");
  if (ids.length === 1) return `CO-${ids[0]}`;
  if (ids.length > 1) return `CO-${ids.slice(0, 6).join("-")}${ids.length > 6 ? "+" : ""}`;
  // No checkout id(s) yet -- e.g. a receipt shown immediately after
  // DispatchModal's POST succeeds, before anything re-fetches the list
  // that would carry one back. Falls back to a timestamp-based reference
  // so there's still something scannable on the ticket.
  return `RCPT-${Date.now().toString(36).toUpperCase()}`;
}

export { safeDateLabel };
