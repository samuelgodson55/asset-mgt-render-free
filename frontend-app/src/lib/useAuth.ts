// Convenience hook for consuming the auth context set up by
// `<AuthProvider>` (see ./auth.tsx). Split into its own file (rather than
// living alongside the provider in auth.tsx) so React Fast Refresh can
// treat the provider component and this plain-function hook separately --
// mixing component and non-component exports in one file breaks Fast
// Refresh's ability to hot-reload without losing state.
import { useContext } from "react";
import { AuthContext } from "./auth-context";

export function useAuth() {
  const ctx = useContext(AuthContext);
  // Throwing here (instead of returning null/undefined) turns "forgot to
  // wrap this tree in <AuthProvider>" into an immediate, obvious error at
  // the call site instead of a confusing downstream null-reference crash.
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}
