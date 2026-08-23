/**
 * lib/api/diagnostics.ts
 */

import type { DbPoolDiagnostics } from "../types";
import { rawFetch } from "./core";

export const diagnosticsApi = {
  dbPool: () => rawFetch<DbPoolDiagnostics>("/diagnostics/db-pool"),
};
