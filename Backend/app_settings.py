"""App-wide settings admins change in the app (user menu → Settings).

Each setting has a type, limits and a default. Defaults come from Backend/.env where a
matching variable existed before (e.g. LOGIN_MAX_FAILURES), so a server keeps behaving the
same until an admin saves something. A saved value (table app_settings) wins over .env.

Values are cached for CACHE_SECONDS per backend process, so reading a setting on every
request is cheap; another process (e.g. the assistant container) sees a change within that.
Secrets and connection details (JWT_SECRET_KEY, DATABASE_URL, passwords...) are never settings.
"""
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

import database
import models

CACHE_SECONDS = 5

# Who may read a setting: "public" (sign-in page, no sign-in needed), "user" (anyone signed
# in), "admin". Only admins can change any of them.
PUBLIC, USER, ADMIN = "public", "user", "admin"

CURRENCIES = {  # code: (label, locale used to show amounts)
    "PHP": ("Philippine peso (₱)", "en-PH"),
    "USD": ("US dollar ($)", "en-US"),
    "EUR": ("Euro (€)", "en-IE"),
    "JPY": ("Japanese yen (¥)", "ja-JP"),
    "SGD": ("Singapore dollar (S$)", "en-SG"),
    "HKD": ("Hong Kong dollar (HK$)", "en-HK"),
    "AUD": ("Australian dollar (A$)", "en-AU"),
    "GBP": ("British pound (£)", "en-GB"),
}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


@dataclass
class Setting:
    key: str
    kind: str                 # "int" | "bool" | "text" | "choice" | "list" (comma-separated text)
    default: Any
    group: str                # Settings tab: security | requests | system | company
    label: str
    help: str = ""
    read: str = ADMIN
    min: Optional[int] = None
    max: Optional[int] = None
    max_length: int = 200
    choices: Dict[str, str] = field(default_factory=dict)
    zero_or_at_least: Optional[int] = None   # int: 0 (= off), or at least this

    def schema(self) -> dict:
        data = {"key": self.key, "kind": self.kind, "default": self.default, "group": self.group,
                "label": self.label, "help": self.help}
        for name in ("min", "max", "zero_or_at_least"):
            if getattr(self, name) is not None:
                data[name] = getattr(self, name)
        if self.kind in ("text", "list"):
            data["max_length"] = self.max_length
        if self.choices:
            data["choices"] = self.choices
        return data


SETTINGS = [
    # ---------- Security ----------
    Setting("login_max_failures", "int", _env_int("LOGIN_MAX_FAILURES", 5), "security",
            "Wrong passwords before an account is locked",
            "After this many wrong passwords in a row, the account can't sign in for a while.", min=3, max=20),
    Setting("login_lock_minutes", "int", _env_int("LOGIN_LOCK_MINUTES", 15), "security",
            "Lock the account for (minutes)", min=1, max=1440),
    Setting("session_idle_minutes", "int", _env_int("SESSION_IDLE_MINUTES", 30), "security",
            "Sign out after inactivity (minutes)",
            "People are signed out after this long without using the app. Applies from their next sign-in "
            "or within a few minutes of use.", read=USER, min=5, max=480),
    Setting("single_session", "bool", _env_bool("SINGLE_SESSION", True), "security",
            "One signed-in device per account",
            "Signing in on a second device asks the first device to allow it."),
    Setting("single_session_exempt", "list", os.getenv("SINGLE_SESSION_EXEMPT", "").strip(), "security",
            "Accounts that may be signed in on several devices",
            "Usernames, separated by commas (e.g. load-test accounts).", max_length=500),
    Setting("allow_registration", "bool", True, "security", "Allow \"Create account\" on the sign-in page",
            "When off, only admins can add people (existing accounts are not affected).", read=PUBLIC),
    Setting("allow_password_reset", "bool", True, "security", "Allow \"Forgot password\" requests",
            "When off, people ask an admin directly, who can reset passwords in Manage Users.", read=PUBLIC),
    Setting("register_per_ip_per_hour", "int", _env_int("REGISTER_PER_IP_PER_HOUR", 10), "security",
            "New accounts per hour from one address", min=1, max=1000),
    Setting("register_total_per_hour", "int", _env_int("REGISTER_TOTAL_PER_HOUR", 30), "security",
            "New accounts per hour in total", min=1, max=10000),
    Setting("max_waiting_accounts", "int", _env_int("MAX_WAITING_ACCOUNTS", 50), "security",
            "Most accounts waiting for approval",
            "New sign-ups are refused while this many are waiting.", min=1, max=1000),

    # ---------- Requests ----------
    Setting("staff_edits_need_approval", "bool", True, "requests", "Staff edits need an admin's approval",
            "When off, staff edits are applied straight away and recorded in the Activity Log. "
            "Archiving still needs approval.", read=USER),
    Setting("edit_reason_required", "bool", False, "requests", "Staff must give a reason for edit requests",
            read=USER),

    # ---------- System ----------
    Setting("archive_retention_days", "int", 30, "system", "Delete archived items permanently after (days)",
            "Archived projects and entries can be restored for this many days, then they're deleted "
            "automatically. Also applies to items that are already archived.", read=USER, min=1, max=365),
    Setting("activity_log_keep_days", "int", 0, "system", "Delete routine Activity Log rows after (days)",
            "Sign-ins, sign-outs, failed sign-ins, exports and assistant questions older than this are "
            "deleted. Changes to records are always kept. 0 keeps everything.",
            min=0, max=3650, zero_or_at_least=30),
    Setting("chat_access", "choice", "everyone", "system", "Chat assistant",
            read=USER, choices={"everyone": "Everyone", "admins": "Admins only", "off": "Off"}),
    Setting("backup_hour", "int", _env_int("BACKUP_HOUR", 2), "system", "Daily backup time (hour, 0–23)",
            "Philippine time. If the server is off then, the backup runs as soon as it's on.", min=0, max=23),
    Setting("backup_keep_daily", "int", _env_int("BACKUP_KEEP_DAILY", 7), "system", "Daily backups to keep",
            min=1, max=90),
    Setting("backup_keep_weekly", "int", _env_int("BACKUP_KEEP_WEEKLY", 4), "system", "Weekly backups to keep",
            min=0, max=104),
    Setting("backup_keep_monthly", "int", _env_int("BACKUP_KEEP_MONTHLY", 6), "system", "Monthly backups to keep",
            min=0, max=120),

    # ---------- Company ----------
    Setting("company_name", "text", "Calamba Allied Industrial Corporation", "company", "Company name",
            "Shown on the sign-in page, in exports and to the assistant.", read=PUBLIC, max_length=120),
    Setting("company_short_name", "text", "CAIC", "company", "Short name",
            "Used in the browser tab title and by the assistant.", read=PUBLIC, max_length=30),
    Setting("currency", "choice", "PHP", "company", "Currency",
            "How amounts are shown and exported. Amounts already saved are not converted.",
            read=PUBLIC, choices={code: label for code, (label, _) in CURRENCIES.items()}),
]
BY_KEY = {s.key: s for s in SETTINGS}

# Internal values kept in the same table, not shown as settings
LOGO_VERSION_KEY = "logo_version"      # bumps when the logo changes (the app reloads it)
BACKUP_REQUEST_KEY = "backup_request"  # "Back up now": set by the app, cleared by the backup container

_TRUE = ("1", "true", "yes", "on")


def _decode(s: Setting, raw: str) -> Any:
    try:
        if s.kind == "int":
            value = int(raw)
            if (s.min is not None and value < s.min) or (s.max is not None and value > s.max):
                return s.default
            return value
        if s.kind == "bool":
            return raw.strip().lower() in _TRUE
        if s.kind == "choice":
            return raw if raw in s.choices else s.default
        return raw
    except (TypeError, ValueError):
        return s.default


def validate(key: str, value: Any) -> Tuple[Any, str]:
    """Checks a value from the browser. Returns (clean value, stored text) or raises ValueError."""
    s = BY_KEY.get(key)
    if not s:
        raise ValueError(f"Unknown setting '{key}'.")
    if s.kind == "int":
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError(f"{s.label}: enter a whole number.")
        try:
            number = int(str(value).strip())
        except ValueError:
            raise ValueError(f"{s.label}: enter a whole number.")
        if str(value).strip() not in (str(number), f"{number}.0"):
            raise ValueError(f"{s.label}: enter a whole number.")
        if (s.min is not None and number < s.min) or (s.max is not None and number > s.max):
            raise ValueError(f"{s.label}: enter a number from {s.min} to {s.max}.")
        if s.zero_or_at_least and 0 < number < s.zero_or_at_least:
            raise ValueError(f"{s.label}: enter 0 (keep everything) or at least {s.zero_or_at_least}.")
        return number, str(number)
    if s.kind == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{s.label}: must be on or off.")
        return value, "true" if value else "false"
    if s.kind == "choice":
        if value not in s.choices:
            raise ValueError(f"{s.label}: choose one of {', '.join(s.choices.values())}.")
        return value, value
    text = " ".join(str(value or "").split())
    if s.kind == "list":
        items = [x.strip() for x in str(value or "").split(",") if x.strip()]
        if any(not re.match(r"^[A-Za-z0-9._-]{1,50}$", x) for x in items):
            raise ValueError(f"{s.label}: usernames only, separated by commas.")
        text = ",".join(dict.fromkeys(items))
    elif not text:
        raise ValueError(f"{s.label} can't be empty.")
    if len(text) > s.max_length:
        raise ValueError(f"{s.label}: at most {s.max_length} characters.")
    return text, text


# ---------- Reading (cached) ----------

_lock = threading.Lock()
_cache: Dict[str, str] = {}
_loaded_at = 0.0


def clear_cache() -> None:
    global _loaded_at
    with _lock:
        _loaded_at = 0.0


def _rows() -> Dict[str, str]:
    global _cache, _loaded_at
    with _lock:
        if time.monotonic() - _loaded_at < CACHE_SECONDS:
            return _cache
    db = database.SessionLocal()
    try:
        rows = {r.key: r.value for r in db.query(models.AppSetting).all()}
    except Exception:
        db.rollback()
        rows = None  # table missing or database busy: keep what we had (or the defaults)
    finally:
        db.close()
    with _lock:
        if rows is not None:
            _cache = rows
            _loaded_at = time.monotonic()
        return _cache


def get(key: str) -> Any:
    s = BY_KEY[key]
    raw = _rows().get(key)
    return s.default if raw is None else _decode(s, raw)


def raw(key: str, default: str = "") -> str:
    """An internal value (LOGO_VERSION_KEY, BACKUP_REQUEST_KEY)."""
    return _rows().get(key, default)


def exempt_usernames() -> set:
    return {u.strip().lower() for u in str(get("single_session_exempt")).split(",") if u.strip()}


def values(level: str) -> dict:
    """Every setting the reader may see. level: PUBLIC, USER or ADMIN."""
    allowed = {PUBLIC: (PUBLIC,), USER: (PUBLIC, USER), ADMIN: (PUBLIC, USER, ADMIN)}[level]
    data = {s.key: get(s.key) for s in SETTINGS if s.read in allowed}
    data["logo_version"] = raw(LOGO_VERSION_KEY) or None
    return data


def currency_locale() -> str:
    return CURRENCIES.get(get("currency"), CURRENCIES["PHP"])[1]


def money(amount) -> str:
    """'PHP 1,234.50' in the configured currency, for Activity Log summaries."""
    return f"{get('currency')} {amount:,.2f}"
