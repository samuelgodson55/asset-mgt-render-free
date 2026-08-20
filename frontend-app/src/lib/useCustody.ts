// Convenience hook for consuming the custody-drawer context set up by
// `<CustodyProvider>` (see ./custodyContext.tsx). Kept in its own file
// (rather than the provider's own .tsx) so React Fast Refresh can treat
// the component export and this hook export separately -- mixing them in
// one file breaks Fast Refresh's ability to hot-reload without a full
// remount.
import { useContext } from "react";
import { CustodyContext, type CustodyContextValue } from "./custody-context";

export function useCustody(): CustodyContextValue {
  const ctx = useContext(CustodyContext);
  // Fail loudly at the call site if a component tries to open the custody
  // drawer outside the provider's tree, rather than silently no-op-ing.
  if (!ctx) throw new Error("useCustody() must be called within a <CustodyProvider>.");
  return ctx;
}
