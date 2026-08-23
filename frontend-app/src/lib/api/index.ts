/**
 * lib/api/index.ts
 * --------------------
 * Barrel re-export preserving the original `lib/api.ts` public surface --
 * every existing `import { x } from "./api"` / `"../lib/api"` call site
 * keeps working unchanged. See each submodule for the actual
 * implementation:
 *
 *   core.ts       -- HTTP client, error types, DirectoryPage<T>, qs()
 *   auth.ts        -- sign-in/MFA/password reset
 *   mappers.ts      -- backend response shape -> UI type translation
 *   dashboard.ts     -- Dashboard/home aggregation + alert feeds
 *   diagnostics.ts    -- DB pool diagnostics
 *   backup.ts          -- System Backups (Super Admin)
 *   digest.ts           -- Daily Digest Recipients
 *   importApi.ts          -- Inventory CSV import
 *   myItems.ts              -- My Items
 *   extensions.ts             -- Extension requests
 *   profile.ts                  -- My Profile
 *   directory.ts                  -- User/Outsider Directory, Audit, Reports
 *   checkouts.ts                    -- Checkouts page
 *   assets.ts                         -- Asset Inventory + maintenance
 *   quotations.ts                      -- Quotation feature
 *   format.ts                            -- currency/date display helpers
 */

export { DEMO_FLAG_KEY, ApiError, MaintenanceModeError, isDemoMode } from "./core";
export type { DirectoryPage } from "./core";

export type { AuthUser, LoginResult } from "./auth";
export { auth } from "./auth";

export type { AlertFeed } from "./dashboard";
export { alertsApi, api } from "./dashboard";
export { getDueSoonReminderDays } from "./dueSoonReminder";

export { diagnosticsApi } from "./diagnostics";
export { backupApi } from "./backup";
export { digestApi } from "./digest";
export { importApi } from "./importApi";
export { myItemsApi } from "./myItems";
export { extensionsApi } from "./extensions";
export { profileApi } from "./profile";
export { usersApi, outsidersApi, auditApi, reportsApi } from "./directory";
export { checkoutsApi } from "./checkouts";

export type { AssetTypeCreateRequest, AdvancedCheckoutPayload } from "./assets";
export { assetsApi, maintenanceApi } from "./assets";

export { quotationsApi } from "./quotations";

export { setCurrencyCode, formatPrice, relativeTime, formatDate } from "./format";
