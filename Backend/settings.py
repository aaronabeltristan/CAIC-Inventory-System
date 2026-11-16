"""Settings routes (user menu → Settings). The settings themselves are in app_settings.py.

- GET  /settings/public            no sign-in: what the sign-in page needs (company, sign-up on/off)
- GET  /settings                   signed in: the settings you may see; admins also get their
                                   descriptions (labels, limits, choices) to build the page
- PUT  /settings                   admins: change one or more ({key: value}); logged
- GET  /settings/retention-preview admins: what a shorter archive period would delete
- POST /settings/backup-now        admins: ask the backup container for a backup now
- GET/PUT/DELETE /settings/logo    the company logo (anyone may read it)

Every change is written to the Activity Log, which also tells open browsers to reload (live.py).
"""
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, Response, UploadFile, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

import activity
import app_settings
import database
import models
import security

router = APIRouter(prefix="/settings", tags=["Settings"])

MAX_LOGO_BYTES = 1024 * 1024
LOGO_ASSET = "logo"


def _set_raw(db: Session, key: str, value: str, username: str) -> None:
    row = db.get(models.AppSetting, key, with_for_update=True)
    if row:
        row.value = value
        row.updated_by = username
    else:
        db.add(models.AppSetting(key=key, value=value, updated_by=username))


def _for(user: models.User) -> dict:
    level = app_settings.ADMIN if user.is_superuser else app_settings.USER
    data: Dict[str, Any] = {"values": app_settings.values(level)}
    if user.is_superuser:
        data["schema"] = [s.schema() for s in app_settings.SETTINGS]
        data["backup_requested"] = bool(app_settings.raw(app_settings.BACKUP_REQUEST_KEY))
    return data


@router.get("/public")
def public_settings():
    """No sign-in needed: the sign-in page shows the company name and logo, and hides
    Create account / Forgot password when they're turned off."""
    return app_settings.values(app_settings.PUBLIC)


@router.get("")
def read_settings(current_user: models.User = Depends(security.get_current_user)):
    return _for(current_user)


@router.put("")
def update_settings(
    changes: Dict[str, Any] = Body(..., examples=[{"archive_retention_days": 30}]),
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(security.get_current_superuser),
):
    """Admins: changes one or more settings. All are checked first; nothing is saved if any
    value is wrong."""
    if not changes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Nothing to change.")
    clean = {}
    try:
        for key, value in changes.items():
            clean[key] = app_settings.validate(key, value)
    except ValueError as e:
        raise HTTPException(422, str(e))

    diff = {}
    for key, (value, stored) in clean.items():
        old = app_settings.get(key)
        if value != old:
            diff[key] = [old, value]
            _set_raw(db, key, stored, current_user.username)
    if diff:
        labels = ", ".join(f"{app_settings.BY_KEY[k].label}: {_show(o)} → {_show(n)}" for k, (o, n) in diff.items())
        activity.log(db, current_user.username, "settings_update", f"Changed settings: {labels}",
                     "settings", None, {"changes": diff})
        db.commit()
        app_settings.clear_cache()
    return _for(current_user)


def _show(value: Any) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    return str(value) if value not in ("", None) else "(none)"


class RetentionPreview(BaseModel):
    archive_retention_days: int
    projects: int   # archived projects that would be deleted at the next clean-up
    entries: int    # archived entries (on their own) that would be deleted at the next clean-up


@router.get("/retention-preview", response_model=RetentionPreview)
def retention_preview(
    archive_retention_days: int = Query(ge=1, le=365),
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(security.get_current_superuser),
):
    """Admins: how many archived items a shorter period would delete at the next clean-up,
    so Settings can warn before saving."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=archive_retention_days)
    projects = (
        db.query(models.Project)
        .filter(models.Project.is_archived.is_(True), models.Project.archived_at < cutoff)
        .count()
    )
    entries = (
        db.query(models.Disbursement)
        .join(models.Project)
        .filter(models.Disbursement.is_archived.is_(True),
                models.Disbursement.archived_at < cutoff,
                models.Project.is_archived.is_(False))
        .count()
    )
    return {"archive_retention_days": archive_retention_days, "projects": projects, "entries": entries}


@router.post("/backup-now", status_code=status.HTTP_202_ACCEPTED)
def backup_now(
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(security.get_current_superuser),
):
    """Admins: the backup container checks every minute and makes a backup when asked.
    The result shows in the backup status (and the Activity Log) when it's done."""
    if app_settings.raw(app_settings.BACKUP_REQUEST_KEY):
        return {"detail": "A backup was already requested. It starts within a minute."}
    _set_raw(db, app_settings.BACKUP_REQUEST_KEY, f"{current_user.username}:{secrets.token_hex(4)}",
             current_user.username)
    activity.log(db, current_user.username, "backup_requested", "Asked for a backup now", "backup", None)
    db.commit()
    app_settings.clear_cache()
    return {"detail": "Backup requested. It starts within a minute; the result appears below when it's done."}


# ---------- Logo ----------

def _image_type(data: bytes):
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


@router.get("/logo")
def get_logo(db: Session = Depends(database.get_db)):
    """The company logo set in Settings (404 = use the built-in one). The app asks with
    ?v=<logo_version>, so browsers may keep it."""
    row = db.get(models.AppAsset, LOGO_ASSET)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No custom logo")
    return Response(row.content, media_type=row.content_type,
                    headers={"Cache-Control": "public, max-age=86400"})


@router.put("/logo")
def upload_logo(
    file: UploadFile = File(...),
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(security.get_current_superuser),
):
    """Admins: sets the logo (PNG, JPEG or WebP, up to 1 MB)."""
    data = file.file.read(MAX_LOGO_BYTES + 1)
    if len(data) > MAX_LOGO_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "The logo is too large (1 MB at most).")
    kind = _image_type(data)
    if not kind:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Use a PNG, JPEG or WebP picture.")
    row = db.get(models.AppAsset, LOGO_ASSET, with_for_update=True)
    if row is None:
        row = models.AppAsset(key=LOGO_ASSET)
        db.add(row)
    row.content, row.content_type = data, kind
    row.updated_by = current_user.username
    row.updated_at = datetime.now(timezone.utc)
    _set_raw(db, app_settings.LOGO_VERSION_KEY, secrets.token_hex(6), current_user.username)
    activity.log(db, current_user.username, "settings_update", "Changed the company logo", "settings", None)
    db.commit()
    app_settings.clear_cache()
    return _for(current_user)


@router.delete("/logo")
def remove_logo(
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(security.get_current_superuser),
):
    """Admins: goes back to the built-in logo."""
    row = db.get(models.AppAsset, LOGO_ASSET)
    if row is not None:
        db.delete(row)
        setting = db.get(models.AppSetting, app_settings.LOGO_VERSION_KEY)
        if setting is not None:
            db.delete(setting)
        activity.log(db, current_user.username, "settings_update", "Went back to the built-in logo",
                     "settings", None)
        db.commit()
        app_settings.clear_cache()
    return _for(current_user)
