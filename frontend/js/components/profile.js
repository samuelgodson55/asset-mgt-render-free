// =============================================================================
// js/components/profile.js
// -----------------------------------------------------------------------------
// "My Profile" window: lets the currently logged-in user (any role -- Super
// Admin, Manager, Staff, or Customer) view their own account details and
// change their own password, without leaving whichever dashboard they're on.
//
// Opened via the navbar user block (`#navProfileBtn`, wired in main.js) on
// every dashboard page. The modal markup itself (`#profileModal`) is
// identical, copy-pasted into admin.html/manager.html/staff.html/
// customer.html -- since it doesn't depend on anything role-specific, one
// shared component file (this one) drives all four.
// =============================================================================

import { apiRequest } from '../api.js';
import { openModal, closeModal, downloadTextFile } from '../ui.js';
import { getSession } from '../auth.js';

// Human-friendly labels for the raw role strings the backend uses.
// Exported so main.js can reuse the exact same mapping for the navbar's
// quick role label (see main.js's DOMContentLoaded handler) instead of
// drifting out of sync with a second, hand-maintained copy.
export const ROLE_LABELS = {
  super_admin: 'Super Admin',
  admin: 'Admin',
  manager: 'Manager',
  staff: 'Staff',
  customer: 'Customer',
};

function setProfileFormMessage(text, isError) {
  const msgEl = document.getElementById('profileFormMessage');
  if (!msgEl) return;
  msgEl.textContent = text || '';
  msgEl.classList.toggle('hidden', !text);
  msgEl.classList.toggle('text-rose-400', !!isError);
  msgEl.classList.toggle('text-emerald-400', !isError);
}

export async function openProfileModal() {
  // Always fetch a FRESH copy from the server rather than trusting the
  // (possibly hours-old) JWT payload cached in localStorage -- see
  // services/auth_service.py's get_profile() docstring for why.
  setProfileFormMessage('', false);
  const form = document.getElementById('changePasswordForm');
  if (form) form.reset();

  openModal('profileModal');

  try {
    const profile = await apiRequest('/auth/me');
    document.getElementById('profileName').textContent = profile.name;
    document.getElementById('profileEmail').textContent = profile.email;
    document.getElementById('profileUsername').textContent = profile.username || '—';
    document.getElementById('profileRole').textContent = ROLE_LABELS[profile.role] || profile.role;

    // Department / Department Role only apply to some roles (e.g. a
    // Customer or a Super Admin typically has neither set) -- hide the
    // whole row instead of showing an empty/placeholder value.
    //
    // NOTE: this row's every-other-row layout is `flex items-center
    // justify-between` (see the markup), but Tailwind's `hidden` utility
    // is `display: none` -- if `flex` stayed on the element at the same
    // time as `hidden`, the two `display` declarations would fight for
    // priority based purely on which one Tailwind happened to generate
    // later in its stylesheet, which is exactly what caused this row to
    // render misaligned (no flex layout applied) whenever it *was* shown.
    // The fix is the same one ui.js's toggleCapacityEdit() already uses
    // elsewhere in this app: never let 'hidden' and 'flex' both be present
    // on the element at once -- toggle them together, as a pair.
    const deptRow = document.getElementById('profileDepartmentRow');
    if (profile.department || profile.department_role) {
      deptRow.classList.remove('hidden');
      deptRow.classList.add('flex');
      document.getElementById('profileDepartment').textContent =
        [profile.department, profile.department_role].filter(Boolean).join(' · ');
    } else {
      deptRow.classList.add('hidden');
      deptRow.classList.remove('flex');
    }

    // Two-factor authentication is currently required for (and thus only
    // ever relevant to) role == super_admin -- see
    // backend/services/auth_service.py's login() SECURITY note. Reaching
    // this dashboard at all already implies totp_enabled is True for a
    // super_admin (login() won't issue a session otherwise -- see that
    // same function), so there's no "set up 2FA from here" case to
    // handle, only "regenerate my recovery codes".
    const mfaSection = document.getElementById('profileMfaSection');
    if (mfaSection) mfaSection.classList.toggle('hidden', profile.role !== 'super_admin');
  } catch (err) {
    setProfileFormMessage(`Could not load profile: ${err.message}`, true);
  }
}

export async function submitChangePasswordForm(event) {
  event.preventDefault();
  setProfileFormMessage('', false);

  const currentPassword = document.getElementById('currentPasswordInput').value;
  const newPassword = document.getElementById('newPasswordInput').value;
  const confirmPassword = document.getElementById('confirmPasswordInput').value;

  // Client-side checks purely for immediate feedback -- the backend
  // independently re-validates password strength (schemas/auth.py's
  // PasswordUpdateRequest) and re-verifies current_password
  // (services/auth_service.py's update_password) no matter what the
  // browser already checked.
  if (newPassword !== confirmPassword) {
    setProfileFormMessage('New password and confirmation do not match.', true);
    return;
  }

  const session = getSession();
  if (!session) {
    setProfileFormMessage('Your session has expired. Please log in again.', true);
    return;
  }

  try {
    const result = await apiRequest('/auth/update-password', {
      method: 'POST',
      body: JSON.stringify({
        user_id: parseInt(session.user_id, 10),
        current_password: currentPassword,
        new_password: newPassword,
      }),
    });
    setProfileFormMessage(result.message || 'Password updated successfully.', false);
    document.getElementById('changePasswordForm').reset();
  } catch (err) {
    setProfileFormMessage(err.message, true);
  }
}

// -----------------------------------------------------------------------------
// 2FA RECOVERY CODE REGENERATION
// -----------------------------------------------------------------------------
// Two-step flow, mirroring the two modals in admin.html:
//   1. #regenerateRecoveryCodesModal -- re-confirm the current password
//      (same re-confirmation pattern Change Password above already uses),
//      then POST /auth/mfa/recovery-codes/regenerate
//      (backend/services/auth_service.py's regenerate_recovery_codes()).
//   2. #recoveryCodesResultModal -- shows the fresh batch EXACTLY ONCE,
//      same "no view-again anywhere" rule as initial enrollment (see
//      backend/models.py's RecoveryCode docstring) -- with the same
//      Download-as-.txt option js/main.js's login-page equivalent has.
// Held in module scope (not a DOM data attribute) for the same reason
// js/main.js's pendingRecoveryCodes is: it's sensitive, single-use data
// that shouldn't linger anywhere more persistent than "this tab, right
// now" -- cleared the moment the result modal closes.
let pendingRegeneratedCodes = null;

function setRegenerateCodesFormMessage(text, isError) {
  const msgEl = document.getElementById('regenerateCodesFormMessage');
  if (!msgEl) return;
  msgEl.textContent = text || '';
  msgEl.classList.toggle('hidden', !text);
  msgEl.classList.toggle('text-rose-400', !!isError);
  msgEl.classList.toggle('text-emerald-400', !isError);
}

export function openRegenerateRecoveryCodesModal() {
  const form = document.getElementById('regenerateRecoveryCodesForm');
  if (form) form.reset();
  setRegenerateCodesFormMessage('', false);
  openModal('regenerateRecoveryCodesModal');
}

export async function submitRegenerateRecoveryCodesForm(event) {
  event.preventDefault();
  setRegenerateCodesFormMessage('', false);
  const password = document.getElementById('regenerateCodesPasswordInput').value;

  try {
    const result = await apiRequest('/auth/mfa/recovery-codes/regenerate', {
      method: 'POST',
      body: JSON.stringify({ password }),
    });
    closeModal('regenerateRecoveryCodesModal');
    pendingRegeneratedCodes = result.recovery_codes;
    const list = document.getElementById('regeneratedRecoveryCodesList');
    if (list) {
      // Plain text nodes, not innerHTML -- same reasoning as
      // js/main.js's showRecoveryCodesScreen(): these are
      // server-generated from a fixed alphabet, but there's no reason
      // to risk it either way.
      list.replaceChildren(
        ...pendingRegeneratedCodes.map((code) => {
          const span = document.createElement('span');
          span.textContent = code;
          return span;
        }),
      );
    }
    openModal('recoveryCodesResultModal');
  } catch (err) {
    setRegenerateCodesFormMessage(err.message, true);
  }
}

export function downloadRegeneratedRecoveryCodes() {
  if (!pendingRegeneratedCodes) return;
  const text = [
    'Snipe-IT Lite -- 2FA recovery codes',
    'Each code works ONCE. Store this file somewhere safe (a password manager, not your Downloads folder long-term).',
    '',
    ...pendingRegeneratedCodes,
    '',
  ].join('\n');
  downloadTextFile('snipeit-lite-recovery-codes.txt', text);
}

export function closeRecoveryCodesResultModal() {
  pendingRegeneratedCodes = null;
  closeModal('recoveryCodesResultModal');
}
