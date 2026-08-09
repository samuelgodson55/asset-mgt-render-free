import { useEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { Bell, X } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { alertsApi, extensionsApi, myItemsApi, formatDate } from "../lib/api";
import { useAuth } from "../lib/useAuth";
import { isPrivileged } from "../lib/roles";
import type { Checkout, ExtensionRequest, MyExtensionDecision, MyItem } from "../lib/types";

// =============================================================================
// components/NotificationBell.tsx
// -----------------------------------------------------------------------------
// Ports js/components/notifications.js -- a header bell dropdown replacing
// the old always-on dashboard banners. Closed by default; refreshes every
// section it has data for the moment it's opened (plus once quietly on
// mount, so the badge count is right even before anyone opens it).
//
// WHO SEES WHAT: the review-facing sections (Overdue Checkouts, Due Soon,
// Extension Requests awaiting a decision) only render for a privileged
// role (Super Admin/Admin/Manager) -- same require_privileged_role gate as
// the backend endpoints they call. The personal sections (My overdue / My
// due soon / My pending requests / My Extension Decisions) render for
// EVERYONE, since any signed-in account can have its own checked-out items.
//
// CLICK-THROUGH: a grouped admin-facing row navigates straight into that
// person's Custody Ledger via a `?custody=type:id&name=...` query param on
// /admin or /manager (see pages/Admin.tsx's AdminOrManagerPage, which reads
// it and opens the drawer). A personal Overdue/Due Soon row navigates to
// /my-items with `?extend=<checkout_id>`, which opens the Request Extension
// modal directly for that item.
// =============================================================================

const MAX_DISMISSED_IDS = 300;
const DECISIONS_DISMISSED_KEY = "ledger:myExtensionDecisionsDismissed";

function readDismissedSet(): Set<number> {
  try {
    const raw = window.localStorage.getItem(DECISIONS_DISMISSED_KEY);
    return new Set(raw ? (JSON.parse(raw) as number[]) : []);
  } catch {
    return new Set();
  }
}

function dismissDecisionIds(ids: number[]) {
  if (!ids.length) return;
  try {
    const merged = readDismissedSet();
    ids.forEach((id) => merged.add(id));
    const trimmed = Array.from(merged).slice(-MAX_DISMISSED_IDS);
    window.localStorage.setItem(DECISIONS_DISMISSED_KEY, JSON.stringify(trimmed));
  } catch {
    // Worst case a dismissal only lasts this page load -- same "ignore
    // storage errors" rule as the legacy frontend's dismissItems().
  }
}

interface PersonGroup {
  entityId: number | null;
  entityType: "user" | "outsider" | null;
  name: string;
  count: number;
}

function groupByPerson(items: Array<{ entity_id?: number | null; entity_type?: "user" | "outsider" | null; name: string }>): PersonGroup[] {
  const groups = new Map<string, PersonGroup>();
  for (const item of items) {
    const key = item.entity_id != null && item.entity_type ? `${item.entity_type}:${item.entity_id}` : `name:${item.name}`;
    const existing = groups.get(key);
    if (existing) existing.count += 1;
    else groups.set(key, { entityId: item.entity_id ?? null, entityType: item.entity_type ?? null, name: item.name, count: 1 });
  }
  return Array.from(groups.values());
}

function SectionHeading({ children }: { children: React.ReactNode }) {
  return <p className="text-[10.5px] uppercase tracking-wider text-text-faint px-1 mb-1.5">{children}</p>;
}

function GroupedRow({ group, color, suffix, onClick }: { group: PersonGroup; color: string; suffix: string; onClick?: () => void }) {
  const clickable = !!onClick;
  return (
    <li
      onClick={onClick}
      className={`flex items-center justify-between gap-3 px-1 py-1.5 rounded-[2px] ${clickable ? "cursor-pointer hover:bg-surface-raised transition-colors" : ""}`}
    >
      <span className="truncate text-[12px] text-text">
        <span className="font-medium">{group.name}</span>
        <span className="text-text-faint"> has {group.count} {suffix}{group.count === 1 ? "" : "s"}</span>
      </span>
      {clickable && <span className={`shrink-0 text-[11px] font-semibold ${color}`}>View →</span>}
    </li>
  );
}

export function NotificationBell() {
  const { user, demo } = useAuth();
  const navigate = useNavigate();
  const ref = useRef<HTMLDivElement>(null);

  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);

  const [overdue, setOverdue] = useState<{ items: Checkout[]; total: number }>({ items: [], total: 0 });
  const [dueSoon, setDueSoon] = useState<{ items: Checkout[]; total: number }>({ items: [], total: 0 });
  const [extensions, setExtensions] = useState<ExtensionRequest[]>([]);
  const [myItems, setMyItems] = useState<MyItem[]>([]);
  const [decisions, setDecisions] = useState<MyExtensionDecision[]>([]);

  const privileged = demo || isPrivileged(user?.role);
  const adminBase = user?.role === "manager" ? "/manager" : "/admin";

  const refresh = async () => {
    setLoading(true);
    const dismissed = readDismissedSet();
    const tasks: Promise<void>[] = [
      myItemsApi.list().then((d) => setMyItems(d.assigned_items)).catch(() => setMyItems([])),
      extensionsApi.myDecisions(10).then((items) => setDecisions(items.filter((d) => !dismissed.has(d.id)))).catch(() => setDecisions([])),
    ];
    if (privileged) {
      tasks.push(alertsApi.overdue(5).then(setOverdue));
      tasks.push(alertsApi.dueSoon(5).then(setDueSoon));
      tasks.push(extensionsApi.listPending().then((items) => setExtensions(items.filter((e) => e.status === "pending"))));
    } else {
      setOverdue({ items: [], total: 0 });
      setDueSoon({ items: [], total: 0 });
      setExtensions([]);
    }
    await Promise.all(tasks);
    setLoading(false);
  };

  // Quiet refresh on mount (for the badge count) plus every time the
  // dropdown is opened (for the freshest picture), same split as legacy's
  // initNotificationBell()/toggleNotificationDropdown().
  useEffect(() => {
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (open) refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  useEffect(() => {
    const onClickOutside = (e: MouseEvent) => {
      if (open && ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onEscape = (e: KeyboardEvent) => {
      if (e.key === "Escape" && open) setOpen(false);
    };
    document.addEventListener("mousedown", onClickOutside);
    document.addEventListener("keydown", onEscape);
    return () => {
      document.removeEventListener("mousedown", onClickOutside);
      document.removeEventListener("keydown", onEscape);
    };
  }, [open]);

  const myOverdue = myItems.filter((i) => i.overdue);
  const myDueSoon = myItems.filter((i) => i.due_soon && !i.overdue);
  const myPending = myItems.filter((i) => i.pending_extension);

  const totalCount =
    overdue.total + dueSoon.total + extensions.length + myOverdue.length + myDueSoon.length + myPending.length + decisions.length;

  const openCustody = (entityId: number | null, entityType: "user" | "outsider" | null, name: string) => {
    if (entityId == null || !entityType) return;
    setOpen(false);
    navigate(`${adminBase}?custody=${entityType}:${entityId}&name=${encodeURIComponent(name)}`);
  };

  const openExtensionRequest = (item: MyItem) => {
    setOpen(false);
    navigate(`/my-items?extend=${item.checkout_id}`);
  };

  const dismissDecision = (id: number) => {
    dismissDecisionIds([id]);
    setDecisions((prev) => prev.filter((d) => d.id !== id));
  };

  const overdueGroups = groupByPerson(overdue.items.map((c) => ({ entity_id: c.entity_id, entity_type: c.entity_type, name: c.checked_out_to })));
  const dueSoonGroups = groupByPerson(dueSoon.items.map((c) => ({ entity_id: c.entity_id, entity_type: c.entity_type, name: c.checked_out_to })));
  const extensionGroups = groupByPerson(extensions.map((e) => ({ entity_id: e.entity_id, entity_type: e.entity_type, name: e.assignee_name ?? e.requested_by })));

  const nothingToShow = totalCount === 0 && !loading;

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        title="Notifications"
        className="relative p-1.5 rounded-[3px] hover:bg-surface transition-colors text-text-faint hover:text-text"
      >
        <Bell size={15} strokeWidth={1.75} />
        {totalCount > 0 && (
          <span className="absolute -top-0.5 -right-0.5 min-w-[15px] h-[15px] px-[3px] rounded-full bg-brass text-ink text-[9px] font-bold flex items-center justify-center leading-none">
            {totalCount > 99 ? "99+" : totalCount}
          </span>
        )}
      </button>

      <AnimatePresence>
        {open && (
          <motion.div
            initial={{ opacity: 0, y: -6, scale: 0.98 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: -4, scale: 0.98 }}
            transition={{ duration: 0.15 }}
            className="absolute right-0 top-full mt-2 z-30 w-[min(92vw,24rem)] max-h-[75vh] overflow-y-auto bg-surface border border-border-soft rounded-[4px] shadow-xl p-3"
          >
            <div className="flex items-center justify-between px-1 mb-2">
              <p className="font-display text-[13px] font-semibold text-text">Notifications</p>
              <button onClick={() => setOpen(false)} className="text-text-faint hover:text-text transition-colors"><X size={14} /></button>
            </div>

            {loading && <p className="text-[12px] text-text-faint text-center py-6">Loading…</p>}

            {!loading && nothingToShow && (
              <p className="text-[12px] text-text-faint text-center py-8">All caught up — nothing needs your attention.</p>
            )}

            {!loading && privileged && overdueGroups.length > 0 && (
              <div className="mb-3">
                <SectionHeading>Overdue checkouts ({overdue.total})</SectionHeading>
                <ul className="flex flex-col">
                  {overdueGroups.map((g) => (
                    <GroupedRow key={`${g.entityType}:${g.entityId}:${g.name}`} group={g} color="text-rust-soft" suffix="overdue checkout" onClick={g.entityId != null ? () => openCustody(g.entityId, g.entityType, g.name) : undefined} />
                  ))}
                </ul>
              </div>
            )}

            {!loading && privileged && dueSoonGroups.length > 0 && (
              <div className="mb-3">
                <SectionHeading>Due soon ({dueSoon.total})</SectionHeading>
                <ul className="flex flex-col">
                  {dueSoonGroups.map((g) => (
                    <GroupedRow key={`${g.entityType}:${g.entityId}:${g.name}`} group={g} color="text-brass-soft" suffix="item due soon" onClick={g.entityId != null ? () => openCustody(g.entityId, g.entityType, g.name) : undefined} />
                  ))}
                </ul>
              </div>
            )}

            {!loading && privileged && extensionGroups.length > 0 && (
              <div className="mb-3">
                <SectionHeading>Extension requests ({extensions.length})</SectionHeading>
                <ul className="flex flex-col">
                  {extensionGroups.map((g) => (
                    <GroupedRow key={`${g.entityType}:${g.entityId}:${g.name}`} group={g} color="text-sky" suffix="pending extension request" onClick={g.entityId != null ? () => openCustody(g.entityId, g.entityType, g.name) : undefined} />
                  ))}
                </ul>
              </div>
            )}

            {!loading && myOverdue.length > 0 && (
              <div className="mb-3">
                <SectionHeading>Your overdue items</SectionHeading>
                <ul className="flex flex-col">
                  {myOverdue.map((item) => (
                    <li key={item.checkout_id} onClick={() => openExtensionRequest(item)} className="flex items-center justify-between gap-3 px-1 py-1.5 rounded-[2px] cursor-pointer hover:bg-surface-raised transition-colors">
                      <span className="truncate text-[12px] text-text">{item.asset_name} <span className="text-text-faint">· due {formatDate(item.due_date)}</span></span>
                      <span className="shrink-0 text-[11px] font-semibold text-rust-soft">Request extension →</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {!loading && myDueSoon.length > 0 && (
              <div className="mb-3">
                <SectionHeading>Your items due soon</SectionHeading>
                <ul className="flex flex-col">
                  {myDueSoon.map((item) => (
                    <li key={item.checkout_id} onClick={() => openExtensionRequest(item)} className="flex items-center justify-between gap-3 px-1 py-1.5 rounded-[2px] cursor-pointer hover:bg-surface-raised transition-colors">
                      <span className="truncate text-[12px] text-text">{item.asset_name} <span className="text-text-faint">· due {formatDate(item.due_date)}</span></span>
                      <span className="shrink-0 text-[11px] font-semibold text-brass-soft">Request extension →</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {!loading && myPending.length > 0 && (
              <div className="mb-3">
                <SectionHeading>Your pending extension requests</SectionHeading>
                <ul className="flex flex-col">
                  {myPending.map((item) => (
                    <li key={item.checkout_id} className="px-1 py-1.5 text-[12px] text-text">
                      {item.asset_name} <span className="text-text-faint">· awaiting a decision</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {!loading && decisions.length > 0 && (
              <div>
                <SectionHeading>Extension decisions</SectionHeading>
                <ul className="flex flex-col gap-0.5">
                  {decisions.map((d) => {
                    const approved = d.status === "approved";
                    return (
                      <li key={d.id} className="flex items-start justify-between gap-2 px-1 py-1.5">
                        <p className="min-w-0 text-[12px] leading-snug">
                          <span className={`font-semibold ${approved ? "text-moss-soft" : "text-rust-soft"}`}>{approved ? "Approved" : "Denied"}:</span>{" "}
                          <span className="text-text">{d.asset_name}</span>{" "}
                          <span className="text-text-faint">
                            {approved ? `— new due date ${formatDate(d.due_date ?? d.requested_new_due_date ?? "")}` : "— current due date unchanged"}
                          </span>
                          {d.decision_note && <span className="block text-text-faint italic mt-0.5">"{d.decision_note}"</span>}
                        </p>
                        <button onClick={() => dismissDecision(d.id)} className="shrink-0 text-text-faint hover:text-text transition-colors mt-0.5"><X size={12} /></button>
                      </li>
                    );
                  })}
                </ul>
              </div>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
