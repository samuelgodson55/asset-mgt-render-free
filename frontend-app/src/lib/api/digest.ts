/**
 * lib/api/digest.ts
 * ---------------------
 * admin: Daily Digest Recipients -- GET/PUT /settings/digest-recipients,
 * Super Admin/Admin only (backend/api/notifications_api.py). Edited as a
 * full list: every add/remove re-PUTs the whole array.
 */

import { mockDigestRecipients } from "../mock";
import { rawFetch, tryLoad } from "./core";

export const digestApi = {
  list: () => tryLoad(async () => (await rawFetch<{ emails: string[] }>("/settings/digest-recipients")).emails ?? [], mockDigestRecipients),
  set: (emails: string[]) => rawFetch<{ emails: string[] }>("/settings/digest-recipients", { method: "PUT", body: JSON.stringify({ emails }) }),
};
