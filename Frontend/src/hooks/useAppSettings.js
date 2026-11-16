import { useSyncExternalStore } from "react";
import { setCurrency } from "../utils/format";

// App-wide settings admins change in Settings. App loads the public ones before sign-in
// (company name, logo, Create account on/off); Dashboard adds the rest after sign-in and
// reloads them on live updates, so a change by an admin shows up everywhere.
export const DEFAULT_SETTINGS = {
  company_name: "Calamba Allied Industrial Corporation",
  company_short_name: "CAIC",
  currency: "PHP",
  logo_version: null,
  allow_registration: true,
  allow_password_reset: true,
  archive_retention_days: 30,
  session_idle_minutes: 30,
  chat_access: "everyone",
  staff_edits_need_approval: true,
  edit_reason_required: false,
};

let values = { ...DEFAULT_SETTINGS };
const listeners = new Set();

export function setAppSettings(next) {
  if (!next) return;
  const merged = { ...values, ...next };
  if (Object.keys(merged).every((k) => merged[k] === values[k])) return;
  values = merged;
  setCurrency(values.currency);
  if (typeof document !== "undefined") document.title = `${values.company_short_name} Dashboard`;
  listeners.forEach((fn) => fn());
}

function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function useAppSettings() {
  return useSyncExternalStore(subscribe, () => values);
}

export const getAppSettings = () => values;
