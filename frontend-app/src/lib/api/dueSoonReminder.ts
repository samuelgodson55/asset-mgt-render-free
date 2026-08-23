/**
 * lib/api/dueSoonReminder.ts
 * -----------------------------
 * Mutable module-level cache of the LAST-SEEN settings.DUE_SOON_REMINDER_DAYS
 * value (.env, echoed back by GET /checkouts -- see backend/services/
 * checkout_service.py's list_active_checkouts()). Set every time a real
 * checkouts fetch actually hits the real endpoint (dashboard.ts's
 * loadCheckouts, checkouts.ts's checkoutsApi.list); read by the Checkouts
 * page to label its "Due soon" column with the true configured window
 * instead of a hardcoded guess. Starts at the backend's own documented
 * default (config.py's DUE_SOON_REMINDER_DAYS) so the column still reads
 * sensibly before the first successful load.
 *
 * Pulled into its own module (rather than a plain exported `let`) because
 * ES modules don't allow a `let` imported into another file to be
 * reassigned from there -- every writer needs to go through
 * setDueSoonReminderDays() instead of `import`-ing and assigning the
 * variable directly.
 */

let lastKnownDueSoonReminderDays = 2;

export function getDueSoonReminderDays(): number {
  return lastKnownDueSoonReminderDays;
}

export function setDueSoonReminderDays(days: number): void {
  lastKnownDueSoonReminderDays = days;
}
