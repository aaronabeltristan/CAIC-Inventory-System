import { setAppSettings, useAppSettings } from "./useAppSettings";

// How many days archived projects and entries can be restored before they're deleted
// permanently (Settings → System). Kept in the app-wide settings store.
export const DEFAULT_RETENTION_DAYS = 30;

export function setRetentionDays(value) {
  const n = Number(value);
  if (Number.isInteger(n) && n >= 1) setAppSettings({ archive_retention_days: n });
}

export function useRetentionDays() {
  return useAppSettings().archive_retention_days;
}

// 1 -> "1 day", 30 -> "30 days"
export const daysLabel = (n) => `${n} day${n === 1 ? "" : "s"}`;
