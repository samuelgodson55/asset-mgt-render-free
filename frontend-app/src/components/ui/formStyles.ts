// =============================================================================
// components/ui/formStyles.ts
// -----------------------------------------------------------------------------
// The single input/select className repeated across every create/edit
// modal's form (CreateUserModal, EditUserModal, ResetPasswordModal,
// RevokeUserModal, EditOutsiderModal, ConvertOutsiderModal, ...). Kept as a
// plain string constant (rather than a wrapper component) since call sites
// need every native input prop -- type, required, min/max, onChange -- with
// nothing abstracted away.
// =============================================================================
export const formInputClass =
  "bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none transition-colors";
