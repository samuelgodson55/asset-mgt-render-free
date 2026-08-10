const config = {
  available: { label: "In stock", dot: "bg-moss", text: "text-moss-soft" },
  low: { label: "Low", dot: "bg-brass", text: "text-brass-soft" },
  out: { label: "Out", dot: "bg-rust", text: "text-rust-soft" },
  active: { label: "Active", dot: "bg-sky", text: "text-sky" },
  overdue: { label: "Overdue", dot: "bg-rust", text: "text-rust-soft" },
  due_soon: { label: "Due soon", dot: "bg-brass", text: "text-brass-soft" },
  returned: { label: "Returned", dot: "bg-moss", text: "text-moss-soft" },
  pending: { label: "Pending", dot: "bg-brass", text: "text-brass-soft" },
  // Quotation lifecycle (draft is never shown as a pill -- see
  // pages/Quotations.tsx/components/QuoteDetailDrawer.tsx): submitted ->
  // approved -> fulfilled, the same wording a customer/staff requester
  // actually sees on their own quote, matching pages/admin/QuotesPanel.tsx's
  // labels so the status reads identically whether an Admin/Manager or the
  // requester themselves is looking at it.
  submitted: { label: "Pending Review", dot: "bg-brass", text: "text-brass-soft" },
  approved: { label: "Approved", dot: "bg-moss", text: "text-moss-soft" },
  fulfilled: { label: "Fulfilled", dot: "bg-sky", text: "text-sky" },
} as const;

export function StatusPill({ status }: { status: keyof typeof config }) {
  const c = config[status];
  return (
    <span className={`inline-flex items-center gap-1.5 text-[11px] font-medium ${c.text} whitespace-nowrap`}>
      <span className={`relative flex h-1.5 w-1.5 rounded-full ${c.dot}`}>
        {(status === "overdue" || status === "out") && (
          <span className={`absolute inline-flex h-full w-full rounded-full ${c.dot} opacity-75 animate-ping`} />
        )}
      </span>
      {c.label}
    </span>
  );
}
