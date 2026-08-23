/**
 * lib/api/importApi.ts
 * ------------------------
 * admin: Inventory Import -- POST /assets/import, Super Admin or Admin
 * (deps.require_super_admin, the broader "full admin" gate -- unlike
 * backups, this one IS available to a plain `admin` account).
 */

import type { ImportResult } from "../types";
import { rawFetchMultipart } from "./core";

export const importApi = {
  importCsv: (file: File) => {
    const formData = new FormData();
    formData.append("file", file);
    return rawFetchMultipart<ImportResult>("/assets/import", formData);
  },
};
