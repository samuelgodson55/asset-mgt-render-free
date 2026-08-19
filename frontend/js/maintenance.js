// -----------------------------------------------------------------------------
// Legacy frontend maintenance-mode UI
// -----------------------------------------------------------------------------
// The backend middleware is the authoritative security boundary.  This module
// is only responsible for the browser experience: showing the maintenance
// overlay/banner, refreshing its state, and exposing the Super Admin controls.
// Never rely on this file to protect an API endpoint; every protected request
// is enforced server-side as well.
// -----------------------------------------------------------------------------

import { API_URL } from './api.js';
import { getSession } from './auth.js';

let timer = null;

/**
 * Read the public deployment configuration used by the maintenance screen.
 * This endpoint intentionally remains available while maintenance is active.
 */
export async function getMaintenanceStatus() {
  const response = await fetch(`${API_URL}/config/public`, {
    credentials: 'include',
  });
  if (!response.ok) {
    throw new Error('Unable to check maintenance status');
  }
  return response.json();
}

/** Identify the backend's maintenance response without trusting only status. */
export function isMaintenanceError(response, body) {
  return response?.status === 503 && body?.code === 'MAINTENANCE_MODE';
}

/**
 * Escape the administrator-supplied maintenance message before putting it
 * into the generated overlay markup.  textContent is used as the escaping
 * primitive so a message containing HTML cannot become executable markup.
 */
function escapeHtml(value) {
  const element = document.createElement('div');
  element.textContent = value;
  return element.innerHTML;
}

/** Render the non-Super-Admin maintenance screen. */
function overlay(config) {
  let element = document.getElementById('maintenanceOverlay');
  if (!element) {
    element = document.createElement('div');
    element.id = 'maintenanceOverlay';
    element.className = 'fixed inset-0 z-[9999] flex items-center justify-center bg-background p-6';
    document.body.appendChild(element);
  }

  const message = escapeHtml(
    config.maintenance_message ||
      'We are currently performing scheduled maintenance. Please check back shortly.',
  );

  element.innerHTML = `
    <section class="w-full max-w-xl rounded-2xl border border-border bg-card p-8 text-center shadow-2xl">
      <div class="mx-auto mb-5 flex h-16 w-16 items-center justify-center rounded-2xl border border-amber-500/30 bg-amber-500/10 text-amber-400 text-3xl">⚙</div>
      <div class="mb-3 inline-flex items-center gap-2 rounded-full border border-amber-500/25 bg-amber-500/10 px-3 py-1 text-xs font-semibold text-amber-300">Maintenance in progress</div>
      <h1 class="text-3xl font-semibold">We’re improving things</h1>
      <p class="mx-auto mt-4 max-w-md text-sm leading-6 text-muted-foreground">${message}</p>
      <p class="mt-4 text-xs text-muted-foreground">Your data remains safe and unchanged.</p>
      <button id="maintenanceRefresh" class="mt-7 rounded-lg border border-border px-4 py-2 text-sm font-medium hover:bg-muted">Refresh status</button>
      <div class="mt-8 border-t border-border pt-5 text-xs text-muted-foreground">
        <a href="/?maintenance_admin=1" class="hover:text-foreground">Administrator sign in</a>
      </div>
    </section>
  `;

  document
    .getElementById('maintenanceRefresh')
    ?.addEventListener('click', () => location.reload());
}

/**
 * Initialize the maintenance presentation on page load.
 *
 * `maintenance_admin=1` is a deliberate UI escape hatch for the login page;
 * it does not grant authorization.  The backend still requires the actual
 * Super Admin credentials/session before allowing maintenance administration.
 *
 * Scoped to the login page specifically (detected the same way the rest of
 * main.js's bootstrap does: presence of #login-form), not honored on
 * admin/manager/staff/customer.html -- otherwise the same query param would
 * silently suppress the maintenance overlay on any page in the bundle, not
 * just the one it's meant for. Real data stays protected server-side
 * either way; this only affects whether the client-side overlay renders.
 */
export async function initMaintenanceMode() {
  const onLoginPage = !!document.getElementById('login-form');
  if (onLoginPage && new URLSearchParams(location.search).has('maintenance_admin')) {
    return false;
  }

  try {
    const config = await getMaintenanceStatus();
    const session = getSession();

    if (config.maintenance_mode && session?.role !== 'super_admin') {
      overlay(config);
      if (!timer) {
        // Periodic refresh lets a waiting user leave the maintenance screen
        // automatically once the administrator disables maintenance.
        timer = setInterval(() => location.reload(), 60000);
      }
      return true;
    }

    if (config.maintenance_mode && session?.role === 'super_admin') {
      const banner = document.createElement('div');
      banner.id = 'maintenanceAdminBanner';
      banner.className =
        'fixed top-0 left-0 right-0 z-[9998] border-b border-amber-300 bg-amber-50 px-4 py-2 text-center text-xs text-amber-950 dark:border-amber-700/60 dark:bg-amber-950/40 dark:text-amber-100';
      banner.innerHTML =
        '<strong>Maintenance mode is active.</strong> Other users cannot access the application.';
      document.body.prepend(banner);
    }

    return false;
  } catch (error) {
    // Maintenance UI must never become a second outage.  If the public
    // status endpoint is unavailable, continue with the normal application;
    // the backend remains the authoritative enforcement layer.
    console.warn('Maintenance status unavailable; continuing normally.', error);
    return false;
  }
}

/** Wire the Super Admin's maintenance toggle on the admin page. */
export async function initMaintenanceControls() {
  const toggle = document.getElementById('maintenanceModeToggle');
  const messageInput = document.getElementById('maintenanceModeMessage');
  if (!toggle || !messageInput) return;

  const session = getSession();
  if (session?.role !== 'super_admin') {
    document.getElementById('maintenanceModeSection')?.classList.add('hidden');
    return;
  }

  const liveIndicator = document.getElementById('maintenanceModeLive');
  let enabled = false;

  const paint = () => {
    toggle.textContent = enabled ? 'Disable Maintenance' : 'Enable Maintenance';
    liveIndicator?.classList.toggle('hidden', !enabled);
  };

  try {
    const response = await fetch(`${API_URL}/maintenance/status`, {
      credentials: 'include',
    });
    const status = await response.json();
    enabled = !!status.enabled;
    messageInput.value = status.message || messageInput.value;
    paint();
  } catch (error) {
    console.warn('Unable to load maintenance controls.', error);
  }

  toggle.addEventListener('click', async () => {
    const next = !enabled;
    const confirmation = next
      ? 'Enable maintenance mode? Other users will immediately be blocked.'
      : 'Disable maintenance mode and restore access?';

    if (!confirm(confirmation)) return;

    toggle.disabled = true;
    try {
      const response = await fetch(`${API_URL}/maintenance/status`, {
        method: 'PUT',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          enabled: next,
          message: messageInput.value,
        }),
      });
      if (!response.ok) {
        throw new Error('Unable to update maintenance mode');
      }

      const status = await response.json();
      enabled = !!status.enabled;
      messageInput.value = status.message;
      paint();
    } catch (error) {
      alert(error.message || 'Unable to update maintenance mode');
    } finally {
      toggle.disabled = false;
    }
  });
}

// A server-side action (for example, another admin disabling maintenance)
// can notify already-open pages.  Non-Super-Admins immediately receive the
// same overlay without requiring the next scheduled page refresh.
window.addEventListener('asset-app:maintenance', async () => {
  try {
    const config = await getMaintenanceStatus();
    if (config.maintenance_mode && getSession()?.role !== 'super_admin') {
      overlay(config);
    }
  } catch {
    // The normal periodic refresh remains the fallback if this notification
    // arrives while the public config endpoint is temporarily unavailable.
  }
});
