// The currency is set in Settings → Company (amounts themselves are never converted)
const LOCALES = { PHP: "en-PH", USD: "en-US", EUR: "en-IE", JPY: "ja-JP", SGD: "en-SG", HKD: "en-HK", AUD: "en-AU", GBP: "en-GB" };
const formatter = (code) =>
  new Intl.NumberFormat(LOCALES[code] || "en-PH", { style: "currency", currency: LOCALES[code] ? code : "PHP" });
let moneyFormat = formatter("PHP");
let currencyCode = "PHP";

export function setCurrency(code) {
  if (!code || code === currencyCode) return;
  currencyCode = code;
  moneyFormat = formatter(code);
}
export const currency = () => currencyCode;

export const money = (n) => moneyFormat.format(Number(n) || 0);
export const cents = (n) => Math.round((Number(n) || 0) * 100);
export const todayISO = () => new Date().toLocaleDateString("en-CA");

export const fmtDate = (iso) =>
  new Date(`${iso}T00:00:00`).toLocaleDateString("en-PH", {
    year: "numeric",
    month: "short",
    day: "numeric",
  });

// "just now", "5 min ago", "3 h ago", "yesterday", then the date
export function timeAgo(iso, now = Date.now()) {
  const seconds = Math.max(0, (now - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  if (seconds < 2 * 86400) return "yesterday";
  return new Date(iso).toLocaleDateString("en-PH", { month: "short", day: "numeric", year: "numeric" });
}

export const fmtStamp = (iso) =>
  new Date(iso).toLocaleString("en-PH", { dateStyle: "medium", timeStyle: "short" });