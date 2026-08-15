import { useEffect, useState } from "react";
import { motion } from "framer-motion";
import { X } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { alertsApi, extensionsApi, myItemsApi, quotationsApi, formatDate } from "../lib/api";
import { useAuth } from "../lib/useAuth";
import { isPrivileged } from "../lib/roles";
import { useRequestGuard } from "../lib/useRequestGuard";
import {
  readDismissedSet,
  dismissDecisionIds,
  readDismissedQuotationNotificationSet,
  dismissQuotationNotificationIds,
} from "../lib/notificationDismissals";
import { useCustody } from "../lib/useCustody";
import { useQuoteDetail } from "../lib/useQuoteDetail";
import type { Checkout, ExtensionRequest, MyExtensionDecision, MyItem, QuotationNotification } from "../lib/types";

// =============================================================================
// pages/Notifications.tsx
// -----------------------------------------------------------------------------
// The full "what needs my attention" page -- what used to live in the header
// Bell's dropdown (components/NotificationBell.tsx) now lives here instead,
// as its own routed page, so it's reachable by URL and has room to breathe.
// The Bell itself is now just a badge that navigates to /notifications on
// tap (see NotificationBell.tsx) -- the two are directly linked.
//
// WHO SEES WHAT: the review-facing sections (Overdue Checkouts, Due Soon,
// Extension Requests awaiting a decision) only render for a privileged role
// (Super Admin/Admin/Manager) -- same require_privileged_role gate as the
// backend endpoints they call. The personal sections (My overdue / My due
// soon / My pending requests / My Extension Decisions) render for EVERYONE,
// since any signed-in account can have its own checked-out items.
//
// CLICK-THROUGH ("View ->" / "Request extension ->"): a grouped admin-facing
// row opens that person's Custody Ledger directly via the shared
// CustodyProvider (see lib/custodyContext.tsx) -- no navigation, same
// "one shared modal, opened by id+type" shape as legacy
// components/custody.js's openCustodyModal(), rather than routing through
// /admin or /manager's own tab state. A personal Overdue/Due Soon row still
// navigates to /my-items with `?extend=<checkout_id>`, which opens the
// Request Extension modal directly for that item. A "Quotation updates" row
// opens the shared "My Quote Detail" drawer the same way the Custody Ledger
// does, via QuoteDetailProvider (see lib/quoteDetailContext.tsx) -- also no
// navigation, so it works the same whether or not Quotations.tsx happens to
// already be mounted.
// =============================================================================

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

function SectionCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3 }}
      className="border border-border-soft bg-surface rounded-[3px] p-4"
    >
      <p className="text-[10.5px] uppercase tracking-wider text-text-faint mb-2.5">{title}</p>
      {children}
    </motion.div>
  );
}

function GroupedRow({ group, color, suffix, onClick }: { group: PersonGroup; color: string; suffix: string; onClick?: () => void }) {
  const clickable = !!onClick;
  return (
    <li
      onClick={onClick}
      className={`flex items-center justify-between gap-3 px-2 py-2 rounded-[2px] ${clickable ? "cursor-pointer hover:bg-surface-raised transition-colors" : ""}`}
    >
      <span className="truncate text-[12.5px] text-text">
        <span className="font-medium">{group.name}</span>
        <span className="text-text-faint"> has {group.count} {suffix}{group.count === 1 ? "" : "s"}</span>
      </span>
      {clickable && <span className={`shrink-0 text-[11.5px] font-semibold ${color}`}>View →</span>}
    </li>
  );
}

export function Notifications() {
  const { user, demo } = useAuth();
  const navigate = useNavigate();
  const { openCustody: openCustodyDrawer } = useCustody();
  const { openQuoteDetail } = useQuoteDetail();

  const [loading, setLoading] = useState(true);
  const [overdue, setOverdue] = useState<{ items: Checkout[]; total: number }>({ items: [], total: 0 });
  const [dueSoon, setDueSoon] = useState<{ items: Checkout[]; total: number }>({ items: [], total: 0 });
  const [extensions, setExtensions] = useState<ExtensionRequest[]>([]);
  const [myItems, setMyItems] = useState<MyItem[]>([]);
  const [decisions, setDecisions] = useState<MyExtensionDecision[]>([]);
  const [quotationNotifications, setQuotationNotifications] = useState<QuotationNotification[]>([]);

  const privileged = demo || isPrivileged(user?.role);
  const beginRequest = useRequestGuard();

  const refresh = () => {
    const isCurrent = beginRequest();
    setLoading(true);
    const dismissed = readDismissedSet();
    const dismissedQuotationNotifications = readDismissedQuotationNotificationSet();
    const tasks: Promise<void>[] = [
      myItemsApi.list().then((d) => { if (isCurrent()) setMyItems(d.assigned_items); }).catch(() => { if (isCurrent()) setMyItems([]); }),
      extensionsApi.myDecisions(10).then((items) => { if (isCurrent()) setDecisions(items.filter((d) => !dismissed.has(d.id))); }).catch(() => { if (isCurrent()) setDecisions([]); }),
      quotationsApi.myNotifications().then((items) => { if (isCurrent()) setQuotationNotifications(items.filter((n) => !dismissedQuotationNotifications.has(n.id))); }).catch(() => { if (isCurrent()) setQuotationNotifications([]); }),
    ];
    if (privileged) {
      tasks.push(alertsApi.overdue(20).then((data) => { if (isCurrent()) setOverdue(data); }).catch((err) => { if (isCurrent()) { console.error("Failed to load overdue alerts:", err); setOverdue({ items: [], total: 0 }); } }));
      tasks.push(alertsApi.dueSoon(20).then((data) => { if (isCurrent()) setDueSoon(data); }).catch((err) => { if (isCurrent()) { console.error("Failed to load due-soon alerts:", err); setDueSoon({ items: [], total: 0 }); } }));
      tasks.push(extensionsApi.listPending().then((items) => { if (isCurrent()) setExtensions(items.filter((e) => e.status === "pending")); }).catch((err) => { if (isCurrent()) { console.error("Failed to load pending extension requests:", err); setExtensions([]); } }));
    } else if (isCurrent()) {
      setOverdue({ items: [], total: 0 });
      setDueSoon({ items: [], total: 0 });
      setExtensions([]);
    }
    Promise.all(tasks).finally(() => { if (isCurrent()) setLoading(false); });
  };
  useEffect(refresh, [privileged]);

  const myOverdue = myItems.filter((i) => i.overdue);
  const myDueSoon = myItems.filter((i) => i.due_soon && !i.overdue);
  const myPending = myItems.filter((i) => i.pending_extension);

  const totalCount =
    overdue.total + dueSoon.total + extensions.length + myOverdue.length + myDueSoon.length + myPending.length + decisions.length + quotationNotifications.length;

  const openCustody = (entityId: number | null, entityType: "user" | "outsider" | null, name: string) => {
    if (entityId == null || !entityType) return;
    openCustodyDrawer(entityType, entityId, name);
  };

  const openExtensionRequest = (item: MyItem) => {
    navigate(`/my-items?extend=${item.checkout_id}`);
  };

  const dismissDecision = (id: number) => {
    dismissDecisionIds([id]);
    setDecisions((prev) => prev.filter((d) => d.id !== id));
  };

  // Client-side only, same as dismissDecision() above -- there's no
  // server-side "delete" for a QuotationNotification (only `read_at`), so
  // a dismissed row just gets remembered in localStorage and filtered out
  // on future loads (see notificationDismissals.ts). Stops propagation so
  // clicking the X doesn't also trigger the row's openQuotationNotification.
  const dismissQuotationNotification = (id: number, e: React.MouseEvent) => {
    e.stopPropagation();
    dismissQuotationNotificationIds([id]);
    setQuotationNotifications((prev) => prev.filter((n) => n.id !== id));
  };

  // Marks it read server-side (so the bell's unread count drops -- see
  // NotificationBell.tsx, which counts `!read_at`) and opens that quote
  // straight away in the shared "My Quote Detail" drawer via
  // QuoteDetailProvider (see lib/quoteDetailContext.tsx) -- no
  // navigation, same "one shared modal, opened by id" shape as this
  // page's Custody Ledger click-through just above.
  const openQuotationNotification = (n: QuotationNotification) => {
    quotationsApi.markNotificationsRead([n.id]).catch(() => {});
    setQuotationNotifications((prev) => prev.map((item) => (item.id === n.id ? { ...item, read_at: new Date().toISOString() } : item)));
    openQuoteDetail(n.quotation_id);
  };

  const overdueGroups = groupByPerson(overdue.items.map((c) => ({ entity_id: c.entity_id, entity_type: c.entity_type, name: c.checked_out_to })));
  const dueSoonGroups = groupByPerson(dueSoon.items.map((c) => ({ entity_id: c.entity_id, entity_type: c.entity_type, name: c.checked_out_to })));
  const extensionGroups = groupByPerson(extensions.map((e) => ({ entity_id: e.entity_id, entity_type: e.entity_type, name: e.assignee_name ?? e.requested_by })));

  const nothingToShow = totalCount === 0 && !loading;

  return (
    <div>
      <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }} className="mb-6">
        <h1 className="font-display text-2xl font-semibold text-text">Notifications</h1>
        <p className="text-text-muted text-sm mt-1">
          {loading ? "Loading…" : nothingToShow ? "All caught up — nothing needs your attention." : `${totalCount} item(s) need your attention.`}
        </p>
      </motion.div>

      {loading && <p className="text-[12px] text-text-faint text-center py-10">Loading…</p>}

      <div className="max-w-2xl flex flex-col gap-3">
        {!loading && privileged && overdueGroups.length > 0 && (
          <SectionCard title={`Overdue checkouts (${overdue.total})`}>
            <ul className="flex flex-col divide-y divide-border-soft">
              {overdueGroups.map((g) => (
                <GroupedRow key={`${g.entityType}:${g.entityId}:${g.name}`} group={g} color="text-rust-soft" suffix="overdue checkout" onClick={g.entityId != null ? () => openCustody(g.entityId, g.entityType, g.name) : undefined} />
              ))}
            </ul>
          </SectionCard>
        )}

        {!loading && privileged && dueSoonGroups.length > 0 && (
          <SectionCard title={`Due soon (${dueSoon.total})`}>
            <ul className="flex flex-col divide-y divide-border-soft">
              {dueSoonGroups.map((g) => (
                <GroupedRow key={`${g.entityType}:${g.entityId}:${g.name}`} group={g} color="text-brass-soft" suffix="item due soon" onClick={g.entityId != null ? () => openCustody(g.entityId, g.entityType, g.name) : undefined} />
              ))}
            </ul>
          </SectionCard>
        )}

        {!loading && privileged && extensionGroups.length > 0 && (
          <SectionCard title={`Extension requests (${extensions.length})`}>
            <ul className="flex flex-col divide-y divide-border-soft">
              {extensionGroups.map((g) => (
                <GroupedRow key={`${g.entityType}:${g.entityId}:${g.name}`} group={g} color="text-sky" suffix="pending extension request" onClick={g.entityId != null ? () => openCustody(g.entityId, g.entityType, g.name) : undefined} />
              ))}
            </ul>
          </SectionCard>
        )}

        {!loading && myOverdue.length > 0 && (
          <SectionCard title="Your overdue items">
            <ul className="flex flex-col divide-y divide-border-soft">
              {myOverdue.map((item) => (
                <li key={item.checkout_id} onClick={() => openExtensionRequest(item)} className="flex items-center justify-between gap-3 px-2 py-2 rounded-[2px] cursor-pointer hover:bg-surface-raised transition-colors">
                  <span className="truncate text-[12.5px] text-text">{item.asset_name} <span className="text-text-faint">· due {formatDate(item.due_date)}</span></span>
                  <span className="shrink-0 text-[11.5px] font-semibold text-rust-soft">Request extension →</span>
                </li>
              ))}
            </ul>
          </SectionCard>
        )}

        {!loading && myDueSoon.length > 0 && (
          <SectionCard title="Your items due soon">
            <ul className="flex flex-col divide-y divide-border-soft">
              {myDueSoon.map((item) => (
                <li key={item.checkout_id} onClick={() => openExtensionRequest(item)} className="flex items-center justify-between gap-3 px-2 py-2 rounded-[2px] cursor-pointer hover:bg-surface-raised transition-colors">
                  <span className="truncate text-[12.5px] text-text">{item.asset_name} <span className="text-text-faint">· due {formatDate(item.due_date)}</span></span>
                  <span className="shrink-0 text-[11.5px] font-semibold text-brass-soft">Request extension →</span>
                </li>
              ))}
            </ul>
          </SectionCard>
        )}

        {!loading && myPending.length > 0 && (
          <SectionCard title="Your pending extension requests">
            <ul className="flex flex-col divide-y divide-border-soft">
              {myPending.map((item) => (
                <li key={item.checkout_id} className="px-2 py-2 text-[12.5px] text-text">
                  {item.asset_name} <span className="text-text-faint">· awaiting a decision</span>
                </li>
              ))}
            </ul>
          </SectionCard>
        )}

        {!loading && quotationNotifications.length > 0 && (
          <SectionCard title={`Quotation updates (${quotationNotifications.length})`}>
            <ul className="flex flex-col divide-y divide-border-soft">
              {quotationNotifications.map((n) => (
                <li
                  key={n.id}
                  onClick={() => openQuotationNotification(n)}
                  className="flex items-start justify-between gap-3 px-2 py-2 rounded-[2px] cursor-pointer hover:bg-surface-raised transition-colors"
                >
                  <div className="min-w-0">
                    <p className="text-[12.5px] leading-snug">
                      {!n.read_at && <span className="inline-block w-1.5 h-1.5 rounded-full bg-brass mr-1.5 align-middle" />}
                      <span className={n.read_at ? "text-text-muted" : "text-text"}>{n.message}</span>
                    </p>
                    {n.reference_number && <p className="text-[11px] text-text-faint font-mono mt-0.5">{n.reference_number}</p>}
                  </div>
                  <span className="shrink-0 flex items-center gap-2.5">
                    <span className="text-[11.5px] font-semibold text-sky">View →</span>
                    <button
                      onClick={(e) => dismissQuotationNotification(n.id, e)}
                      title="Dismiss"
                      className="text-text-faint hover:text-text transition-colors"
                    >
                      <X size={13} />
                    </button>
                  </span>
                </li>
              ))}
            </ul>
          </SectionCard>
        )}

        {!loading && decisions.length > 0 && (
          <SectionCard title="Extension decisions">
            <ul className="flex flex-col gap-0.5">
              {decisions.map((d) => {
                const approved = d.status === "approved";
                return (
                  <li key={d.id} className="flex items-start justify-between gap-2 px-2 py-2">
                    <p className="min-w-0 text-[12.5px] leading-snug">
                      <span className={`font-semibold ${approved ? "text-moss-soft" : "text-rust-soft"}`}>{approved ? "Approved" : "Denied"}:</span>{" "}
                      <span className="text-text">{d.asset_name}</span>{" "}
                      <span className="text-text-faint">
                        {approved ? `— new due date ${formatDate(d.due_date ?? d.requested_new_due_date ?? "")}` : "— current due date unchanged"}
                      </span>
                      {d.decision_note && <span className="block text-text-faint italic mt-0.5">"{d.decision_note}"</span>}
                    </p>
                    <button onClick={() => dismissDecision(d.id)} className="shrink-0 text-text-faint hover:text-text transition-colors mt-0.5"><X size={13} /></button>
                  </li>
                );
              })}
            </ul>
          </SectionCard>
        )}

        {!loading && nothingToShow && (
          <p className="text-[12.5px] text-text-faint text-center py-10">All caught up — nothing needs your attention.</p>
        )}
      </div>
    </div>
  );
}
