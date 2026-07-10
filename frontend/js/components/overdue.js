// =============================================================================
// js/components/overdue.js
// -----------------------------------------------------------------------------
// Operations & Observability requirement #5: renders the "Overdue Checkouts"
// alert banner on admin.html / manager.html from GET /checkouts/overdue.
//
// Kept as its own tiny module (rather than folded into assets.js or
// dashboard.js) because it owns exactly one job -- load the overdue list and
// paint the banner -- and both admin.html and manager.html share the exact
// same markup/ids for it, so this same code works unmodified on both pages
// (the backend already scopes what a Manager vs a Super Admin sees -- see
// services/checkout_service.py's list_overdue_checkouts()).
// =============================================================================

import { apiRequest } from '../api.js';
import { escapeHtml } from '../ui.js';

export async function loadOverdueAlerts() {
  const banner = document.getElementById('overdueAlertBanner');
  if (!banner) return; // this page doesn't have the banner (e.g. staff/customer dashboards)

  try {
    const result = await apiRequest('/checkouts/overdue?limit=5');

    if (!result.total) {
      banner.classList.add('hidden');
      return;
    }

    document.getElementById('overdueAlertCount').textContent = result.total;

    const list = document.getElementById('overdueAlertList');
    list.innerHTML = result.items.map(item => `
      <li class="flex items-center justify-between gap-3 py-1">
        <span class="truncate text-slate-200">
          <span class="font-medium">${escapeHtml(item.asset_name)}</span>
          <span class="text-slate-500"> · ${escapeHtml(item.assignee_name)}</span>
        </span>
        <span class="shrink-0 tag-mono text-[11px] font-semibold text-rose-400">
          ${item.days_overdue} day${item.days_overdue === 1 ? '' : 's'} overdue
        </span>
      </li>`).join('');

    banner.classList.remove('hidden');
  } catch (err) {
    // Fail quietly -- a broken alert banner should never block the rest of
    // the dashboard from loading/working. The other widgets (assets,
    // users, audit log) already reported their own errors independently.
    banner.classList.add('hidden');
    console.error('Failed to load overdue checkouts alert:', err.message);
  }
}
