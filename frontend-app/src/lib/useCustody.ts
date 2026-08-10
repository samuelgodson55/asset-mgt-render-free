import { useContext } from "react";
import { CustodyContext, type CustodyContextValue } from "./custody-context";

export function useCustody(): CustodyContextValue {
  const ctx = useContext(CustodyContext);
  if (!ctx) throw new Error("useCustody() must be called within a <CustodyProvider>.");
  return ctx;
}
