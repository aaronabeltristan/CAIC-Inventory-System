"""Health checks: is each part of the system working? (Diagnostics, see docs/DIAGNOSTICS.md)

- GET /health          no sign-in. {"status": "ok"} with 200, or 503 when the database can't be
                       reached. Used by Docker's healthcheck (docker-compose.yml) and by anyone
                       checking from outside (curl http://<server>/api/health).
- GET /health/details  admins. Every component with a status (ok / warn / fail / off), a short
                       code (e.g. E-DB-01, see CODES) and what to do. Shown in Settings → Status.

Each check is quick and has its own timeout, so one broken part can't hang the others.
"""
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import func, text
from sqlalchemy.orm import Session

import app_settings
import database
import models
import security

router = APIRouter(tags=["Health"])

STARTED = time.time()
BACKUP_STALE_HOURS = 26          # a nightly backup plus some slack (the server may be off at 2 AM)
BACKUP_REQUEST_STALE_MINUTES = 5 # "Back up now" not picked up by then: the backup container isn't running
PURGE_STALE_HOURS = 2            # the clean-up runs every hour
LIVE_STALE_SECONDS = 15          # the live-update watcher checks every 1.5 s
SLOW_DB_MS = 500

# Every code the checks can give, with what it means and what to do. Codes starting with E are
# failures (red), W warnings (yellow). The same list is in docs/DIAGNOSTICS.md.
CODES = {
    "E-DB-01": ("The database can't be reached", "Check the postgres-db container: docker compose -p backend ps; "
                "start it with docker start postgres-db. See its log: docker logs postgres-db"),
    "W-DB-02": ("The database answers slowly", "Look for a big import or export in progress; check the server's CPU and disk"),
    "E-AI-01": ("The assistant's model server can't be reached", "Start it: docker start caic-ollama; see docker logs caic-ollama"),
    "W-AI-02": ("The assistant's model isn't downloaded", "docker compose -p backend exec ollama ollama pull <model>"),
    "E-BK-01": ("The last backup failed", "Read the reason here or in the Activity Log (Backups); check BACKUP_PASSPHRASE "
                "and the backups folder; docker logs caic-backup"),
    "W-BK-02": ("No backup in the last 26 hours", "Is the backup container running? docker start caic-backup; "
                "use Back up now in Settings → System"),
    "W-BK-03": ("Back up now hasn't started after 5 minutes", "The backup container isn't running: docker start caic-backup"),
    "W-PU-01": ("The automatic clean-up hasn't run in 2 hours", "Restart the backend: docker restart caic-api; see docker logs caic-api"),
    "E-LV-01": ("Live updates stopped", "Screens won't refresh by themselves. Restart the backend: docker restart caic-api"),
    "W-ER-01": ("Server errors in the last 24 hours", "Open Activity Log, action Server error: each has an error ID "
                "(E-xxxxxxxx) to find in docker logs caic-api"),
}


def _component(key, label, status, detail, code=None, extra=None):
    data = {"key": key, "label": label, "status": status, "detail": detail}
    if code:
        data["code"] = code
        data["meaning"], data["action"] = CODES[code]
    if extra:
        data.update(extra)
    return data


def _aware(moment: Optional[datetime]) -> Optional[datetime]:
    if moment is not None and moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def _ago(moment: datetime) -> str:
    seconds = (datetime.now(timezone.utc) - _aware(moment)).total_seconds()
    if seconds < 90:
        return f"{int(seconds)} s ago"
    if seconds < 5400:
        return f"{int(seconds // 60)} min ago"
    if seconds < 2 * 86400:
        return f"{seconds / 3600:.1f} h ago"
    return f"{seconds / 86400:.1f} days ago"


# ---------- Checks ----------

def check_database(db: Session) -> dict:
    started = time.perf_counter()
    try:
        db.execute(text("SELECT 1"))
    except Exception as e:
        db.rollback()
        return _component("database", "Database", "fail", f"{type(e).__name__}: {str(e)[:200]}", "E-DB-01")
    ms = (time.perf_counter() - started) * 1000
    if ms > SLOW_DB_MS:
        return _component("database", "Database", "warn", f"Answered in {ms:.0f} ms", "W-DB-02", {"ms": round(ms)})
    return _component("database", "Database", "ok", f"Answered in {ms:.0f} ms", extra={"ms": round(ms)})


def check_assistant() -> dict:
    if app_settings.get("chat_access") == "off":
        return _component("assistant", "Assistant (AI model)", "off", "Turned off in Settings → System")
    host = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    model = os.getenv("OLLAMA_MODEL", "qwen3:4b")
    try:
        with urllib.request.urlopen(f"{host}/api/tags", timeout=2) as r:
            import json
            names = {m.get("name") for m in json.loads(r.read().decode("utf-8")).get("models", [])}
    except Exception as e:
        return _component("assistant", "Assistant (AI model)", "fail", f"{host}: {type(e).__name__}", "E-AI-01")
    if model not in names and f"{model}:latest" not in names:
        return _component("assistant", "Assistant (AI model)", "warn", f"Model {model} isn't downloaded", "W-AI-02")
    return _component("assistant", "Assistant (AI model)", "ok", f"Model {model} ready")


def check_backups(db: Session) -> dict:
    def latest(action):
        return (db.query(models.ActivityLog.created_at, models.ActivityLog.summary)
                .filter(models.ActivityLog.action == action)
                .order_by(models.ActivityLog.created_at.desc()).first())
    ok, failed = latest("backup"), latest("backup_failed")
    requested = db.get(models.AppSetting, app_settings.BACKUP_REQUEST_KEY)
    if requested is not None and requested.value and requested.updated_at is not None:
        waiting = datetime.now(timezone.utc) - _aware(requested.updated_at)
        if waiting > timedelta(minutes=BACKUP_REQUEST_STALE_MINUTES):
            return _component("backups", "Backups", "warn", f"Back up now was asked {_ago(requested.updated_at)}", "W-BK-03")
    if failed and (not ok or _aware(failed[0]) > _aware(ok[0])):
        return _component("backups", "Backups", "fail", f"{failed[1]} ({_ago(failed[0])})", "E-BK-01")
    if not ok:
        return _component("backups", "Backups", "warn", "No backup recorded yet", "W-BK-02")
    if datetime.now(timezone.utc) - _aware(ok[0]) > timedelta(hours=BACKUP_STALE_HOURS):
        return _component("backups", "Backups", "warn", f"Last backup {_ago(ok[0])}", "W-BK-02")
    return _component("backups", "Backups", "ok", f"Last backup {_ago(ok[0])}")


def check_cleanup(db: Session) -> dict:
    if os.getenv("PURGE_ENABLED", "true").strip().lower() in ("0", "false", "no", "off"):
        return _component("cleanup", "Automatic clean-up", "off", "PURGE_ENABLED=false on this backend")
    row = db.get(models.AppSetting, PURGE_LAST_RUN_KEY)
    if row is None or row.updated_at is None:
        if time.time() - STARTED < 300:
            return _component("cleanup", "Automatic clean-up", "ok", "Backend just started")
        return _component("cleanup", "Automatic clean-up", "warn", "No run recorded yet", "W-PU-01")
    if datetime.now(timezone.utc) - _aware(row.updated_at) > timedelta(hours=PURGE_STALE_HOURS):
        return _component("cleanup", "Automatic clean-up", "warn", f"Last run {_ago(row.updated_at)}", "W-PU-01")
    return _component("cleanup", "Automatic clean-up", "ok", f"Last run {_ago(row.updated_at)} ({row.value})")


def check_live() -> dict:
    import live
    hub = live.hub
    if hub.last_ok is None:
        if time.time() - STARTED < 30:
            return _component("live", "Live updates", "ok", "Starting")
        return _component("live", "Live updates", "fail", hub.last_error or "The watcher hasn't run", "E-LV-01")
    age = time.time() - hub.last_ok
    if age > LIVE_STALE_SECONDS:
        return _component("live", "Live updates", "fail",
                          f"Last check {int(age)} s ago" + (f"; {hub.last_error}" if hub.last_error else ""), "E-LV-01")
    return _component("live", "Live updates", "ok", f"Watching ({len(hub.queues)} open screen(s) on this process)")


def check_errors(db: Session) -> dict:
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    query = db.query(models.ActivityLog).filter(models.ActivityLog.action == "server_error",
                                                models.ActivityLog.created_at > since)
    count = query.count()
    if not count:
        return _component("errors", "Server errors (24 h)", "ok", "None")
    last = query.order_by(models.ActivityLog.created_at.desc()).first()
    error_id = (last.details or {}).get("error_id", "?") if isinstance(last.details, dict) else "?"
    return _component("errors", "Server errors (24 h)", "warn",
                      f"{count}; latest {error_id} {_ago(last.created_at)}: {last.summary[:150]}", "W-ER-01",
                      {"count": count, "latest_error_id": error_id})


PURGE_LAST_RUN_KEY = "purge_last_run"


def note_purge_run(db: Session, summary: str) -> None:
    """Called by the hourly clean-up (auth.purge_once): the watchdog's sign of life."""
    row = db.get(models.AppSetting, PURGE_LAST_RUN_KEY)
    if row is None:
        db.add(models.AppSetting(key=PURGE_LAST_RUN_KEY, value=summary[:500], updated_by="system"))
    else:
        row.value = summary[:500]
        row.updated_at = datetime.now(timezone.utc)
    db.commit()


# ---------- Routes ----------

@router.get("/health")
def health(db: Session = Depends(database.get_db)):
    """No sign-in: 200 {"status": "ok"} when the backend and its database work, else 503."""
    result = check_database(db)
    if result["status"] == "fail":
        return JSONResponse({"status": "down", "code": result["code"]}, status_code=503)
    return {"status": "ok"}


@router.get("/health/details")
def health_details(
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(security.get_current_superuser),
):
    """Admins: every component's status, for Settings → Status."""
    components: List[dict] = [check_database(db)]
    if components[0]["status"] != "fail":
        components += [check_backups(db), check_cleanup(db), check_errors(db)]
    components += [check_live(), check_assistant()]
    worst = "ok"
    if any(c["status"] == "fail" for c in components):
        worst = "fail"
    elif any(c["status"] == "warn" for c in components):
        worst = "warn"
    return {
        "status": worst,
        "checked_at": datetime.now(timezone.utc),
        "uptime_seconds": int(time.time() - STARTED),
        "process": os.getpid(),
        "components": components,
        "codes": {code: {"meaning": m, "action": a} for code, (m, a) in CODES.items()},
    }
