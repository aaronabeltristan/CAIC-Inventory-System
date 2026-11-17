import asyncio
import os
import re
import secrets
import traceback
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Annotated, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, status
from sqlalchemy import func
from fastapi.responses import JSONResponse
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

import logtime  # noqa: F401  (first: date and time on the app's own log lines)
import crud
import database
import models
import schemas
import security
import deletion_requests
import activity
import app_settings
import password_resets
import users_admin
import imports
import live
import chatbot
import profiles
import sessions
import bulk
import settings
import purchase_orders
import health

# Create database tables automatically, then add any new columns to existing tables
models.Base.metadata.create_all(bind=database.engine)
database.ensure_schema()
database.flag_default_passwords()

# Deletes anything archived longer ago than the retention period admins set in Settings
# (30 days by default). Runs at startup, then every hour.
# Also deletes old routine Activity Log rows (Settings → System). PURGE_ENABLED=false in
# Backend/.env turns both off: for a local backend pointed at the real database while
# testing, so it can't delete real data.
PURGE_EVERY_SECONDS = 60 * 60
PURGE_ENABLED = os.getenv("PURGE_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")


def purge_once() -> None:
    if not PURGE_ENABLED:
        return
    db = database.SessionLocal()
    try:
        removed = activity.purge_expired(db)
        if removed:
            print(f"[purge] permanently deleted {removed} archived record(s)")
        cleaned = activity.purge_routine_logs(db)
        if cleaned:
            print(f"[purge] deleted {cleaned} old routine Activity Log row(s)")
        # Sign of life for the health check (W-PU-01 if it stops)
        health.note_purge_run(db, f"removed {removed} archived, {cleaned} log row(s)")
    except Exception as e:  # another server may have purged the same rows first
        db.rollback()
        print(f"[purge] skipped: {e}")
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    async def loop():
        while True:
            await run_in_threadpool(purge_once)
            await asyncio.sleep(PURGE_EVERY_SECONDS)

    if not PURGE_ENABLED:
        print("[purge] off (PURGE_ENABLED=false): archived items won't be deleted automatically")
    task = asyncio.create_task(loop())
    watcher = asyncio.create_task(live.hub.run())  # live updates (see live.py)
    yield
    live.hub.close()
    watcher.cancel()
    task.cancel()


app = FastAPI(title="Calamba Allied Industrial Corporation", lifespan=lifespan)
app.include_router(health.router)


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception):
    """Any error the code didn't expect (a bug, or a part of the system that's down).

    The person gets a short error ID instead of a bare "Internal Server Error"; the full
    details go to the backend's log (docker logs caic-api) under the same ID, and a
    "Server error" row goes in the Activity Log, so an admin can match the two."""
    error_id = "E-" + secrets.token_hex(4).upper()
    where = f"{request.method} {request.url.path}"
    print(f"[error {error_id}] {where}\n{''.join(traceback.format_exception(exc))}", flush=True)
    try:
        await run_in_threadpool(_record_server_error, error_id, where, exc)
    except Exception as log_failure:  # e.g. the database is the thing that's down
        print(f"[error {error_id}] couldn't write it to the Activity Log: {log_failure}", flush=True)
    return JSONResponse(status_code=500, content={
        "detail": f"Something went wrong on the server (error {error_id}). Try again; if it keeps "
                  f"happening, give this code to an admin.",
        "error_id": error_id,
    })


def _record_server_error(error_id: str, where: str, exc: Exception) -> None:
    db = database.SessionLocal()
    try:
        activity.log(db, "system", "server_error", f"Server error {error_id} on {where}: {type(exc).__name__}: {exc}"[:500],
                     "system", None, {"error_id": error_id, "where": where, "type": type(exc).__name__,
                                      "message": str(exc)[:500]})
        db.commit()
    finally:
        db.close()

app.include_router(deletion_requests.router)
app.include_router(activity.router)
app.include_router(password_resets.router)
app.include_router(users_admin.router)
app.include_router(imports.router)
app.include_router(live.router)
app.include_router(chatbot.router)
app.include_router(profiles.router)
app.include_router(sessions.router)
app.include_router(bulk.router)
app.include_router(settings.router)
app.include_router(purchase_orders.router)
# Which other websites may call the API from a browser. The app itself doesn't need this: it's
# served from the same address (nginx passes /api/... on). Only developers running
# "npm run dev" against the shared backend do, so by default that's all that's allowed.
# Sign-in uses a token in a header, not cookies, so no credentials are shared with other sites.
CORS_ORIGINS = [o.strip() for o in os.getenv(
    "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

# ==========================================
# AUTHENTICATION & USER ROUTES
# ==========================================


# Sign-up rules (see register_user)
USERNAME_RULE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,49}$")
RESERVED_USERNAMES = {"system", "admin", "administrator", "root", "superuser", "support", "caic",
                      "api", "null", "undefined", "everyone", "staff", "anonymous"}
# Limits on sign-ups: Settings → Security (register_per_ip_per_hour, register_total_per_hour,
# max_waiting_accounts, allow_registration). Defaults from .env, see app_settings.py.


@app.post(
    "/auth/register",
    response_model=schemas.UserResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["Authentication"],
)
def register_user(
    payload: schemas.UserCreate, request: Request, db: Session = Depends(database.get_db)
):
    """Registers a new user account. It can't sign in until an admin approves it."""
    if not app_settings.get("allow_registration"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="New accounts can't be created here right now. Ask an admin to add you.",
        )
    username = payload.username.strip()
    if not USERNAME_RULE.match(username):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Usernames are 3 to 50 letters, numbers, dots, dashes or underscores, "
                   "starting with a letter or number (no spaces).",
        )
    # "Admin" and "admin" would look the same in the Activity Log, so names are unique
    # ignoring capitals; and a few names are kept for the system
    if username.lower() in RESERVED_USERNAMES or db.query(models.User.id).filter(
        func.lower(models.User.username) == username.lower()
    ).first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username is already taken",
        )
    # Sign-ups are open to anyone with the link, so limit how fast accounts can be made
    ip = security.client_ip(request)
    if security.rate_limited(db, "register", ip, per_ip=app_settings.get("register_per_ip_per_hour"),
                             total=app_settings.get("register_total_per_hour"), minutes=60):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many new accounts were made recently. Please try again later or ask an admin.",
        )
    waiting = db.query(func.count(models.User.id)).filter(models.User.is_approved.is_(False)).scalar()
    if waiting >= app_settings.get("max_waiting_accounts"):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many accounts are waiting for approval. Ask an admin to review them first.",
        )
    security.note_event(db, "register", ip)
    payload.username = username
    payload.name = " ".join(payload.name.split())
    user = crud.create_user(db=db, user_in=payload)
    activity.log(db, user.username, "register",
                 f"Registered account '{user.username}' ({user.name}); waiting for approval", "user", user.id,
                 {"ip": ip} if ip else None)
    db.commit()
    return user


@app.post(
    "/token",
    response_model=schemas.Token,
    tags=["Authentication"],
)
def login_for_access_token(
    request: Request,
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
    db: Session = Depends(database.get_db),
):
    """OAuth2 compatible token login, returns a JWT access token for Swagger UI and clients.

    If the account is open on another device, answers 202 {"status": "waiting", ...} instead:
    that device is asked to allow the sign-in (see sessions.py)."""
    # Too many wrong passwords lately: refuse before even checking this one. Not logged, so
    # someone hammering the sign-in page can't flood the Activity Log.
    locked = security.login_locked_minutes(db, form_data.username[:255])
    if locked:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many wrong passwords for this account. Try again in {locked} "
                   f"minute{'s' if locked != 1 else ''}, or use Forgot password.",
            headers={"Retry-After": str(locked * 60)},
        )
    user = crud.authenticate_user(
        db, username=form_data.username, password=form_data.password
    )
    if not user:
        activity.log(db, form_data.username[:255] or "?", "login_failed",
                     f"Failed sign-in attempt for '{form_data.username[:100]}'", "user")
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        activity.log(db, user.username, "login_failed",
                     f"Sign-in blocked: '{user.username}' is inactive", "user", user.id)
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Inactive user account",
        )

    if not user.is_approved:
        # Only said after a correct password, so it doesn't reveal which usernames exist
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account is waiting for an admin's approval. Try again once it's approved.",
        )

    # Signed in on another device that's still there: ask that device first
    if security.single_session_applies(user) and sessions.session_is_live(user):
        if sessions.too_many_challenges(db, user):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many sign-in attempts while this account is open on another device. "
                       "Wait a few minutes, or sign out on the other device first.",
            )
        challenge = sessions.create_challenge(db, user, request.headers.get("user-agent", ""))
        db.commit()
        return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content={
            "status": "waiting",
            "challenge_id": challenge.id,
            "secret": challenge.secret,
            "expires_in": sessions.CHALLENGE_SECONDS,
            "detail": "This account is signed in on another device. That device has been asked to let you in.",
        })

    access_token = sessions.issue_token(user, sessions.start_session(user))
    activity.log(db, user.username, "login", f"'{user.username}' signed in", "user", user.id)
    db.commit()
    return {"access_token": access_token, "token_type": "bearer"}


@app.post(
    "/users/change-password",
    response_model=schemas.UserResponse,
    tags=["Users"],
)
def update_password(
    payload: schemas.PasswordChangeSchema,
    current_user: models.User = Depends(security.get_current_user_allow_temporary),
    db: Session = Depends(database.get_db),
):
    """Changes password for the currently authenticated user. Also how someone signed in
    with a temporary password (after a reset) sets their new one."""
    if not security.verify_password(
        payload.old_password, current_user.hashed_password
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Incorrect existing password",
        )

    was_temporary = current_user.must_change_password
    user = crud.change_password(
        db, username=current_user.username, new_password=payload.new_password
    )
    user.must_change_password = False
    activity.log(db, current_user.username, "password_change",
                 f"'{current_user.username}' " + ("replaced their temporary password" if was_temporary
                                                  else "changed their password"),
                 "user", current_user.id)
    db.commit()
    return user


# ==========================================
# PROJECT ROUTES (PROTECTED)
# ==========================================


@app.get(
    "/projects",
    response_model=List[schemas.ProjectResponse],
    tags=["Projects"],
)
def read_all_projects(
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(security.get_current_user),
):
    """Active projects with their totals (computed by the database) and entry counts."""
    return crud.get_all_projects(db)


# Creating a project (with its purchase order): see purchase_orders.py


@app.delete(
    "/projects/{project_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["Projects"],
)
def archive_project(
    project_id: int,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(security.get_current_superuser),
):
    """Admins only: archives a project together with all its entries. It can be restored
    from the Activity Log for the retention period set in Settings, then it is deleted permanently."""
    project = db.get(models.Project, project_id, with_for_update=True)
    if not project:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    if project.is_archived:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This project is already archived.")

    activity.archive_project(db, project, current_user)
    db.commit()
    return None


# ==========================================
# DISBURSEMENT RELATED ROUTES (PROTECTED)
# ==========================================


@app.get(
    "/disbursements/",
    response_model=List[schemas.DisbursementListItem],
    tags=["Disbursements"],
)
def list_disbursements(
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(security.get_current_user),
):
    """Every active entry with its project title, newest first. Voucher Control loads this in
    one request (it used to ask for each project's summary: 100+ requests per page load)."""
    return crud.get_active_disbursements(db)


@app.post(
    "/disbursements/",
    response_model=schemas.DisbursementItemResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["Disbursements"],
)
def record_disbursement(
    payload: schemas.DisbursementCreate,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(security.get_current_user),
):
    """Appends a new disbursement record to the given project title."""
    existing_project = crud.get_project_by_title(db, title=payload.project_title)
    if existing_project and existing_project.is_archived:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A project with this title is archived. An admin can restore it from Activity Log \u2192 Archived.")
    project_existed = existing_project is not None
    d = crud.create_disbursement(db, payload, current_user)
    if not project_existed:
        activity.log(db, current_user.username, "project_create",
                     f"Created project '{d.project.title}' (with its first entry)", "project", d.project_id)
    activity.log(db, current_user.username, "create", f"Added {activity.describe(d)}",
                 "disbursement", d.id, {"entry": activity.snapshot(d)})
    db.commit()
    db.refresh(d)
    crud.with_person_names(db, [d])
    return d


@app.patch(
    "/disbursements/{disbursement_id}",
    response_model=schemas.DisbursementItemResponse,
    tags=["Disbursements"],
)
def update_disbursement_log(
    disbursement_id: int,
    payload: schemas.DisbursementUpdate,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(security.get_current_superuser),
):
    """Admins only: edits an entry directly. Other users send an edit request instead
    (POST /disbursements/{id}/edit-request), which an admin approves or rejects."""
    existing = db.get(models.Disbursement, disbursement_id, with_for_update=True)  # one edit at a time
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Disbursement log not found",
        )
    if existing.is_archived:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This entry is archived. An admin has to restore it before it can be edited.",
        )

    activity.check_unchanged(existing, payload.original)

    before = activity.snapshot(existing)
    updated_item = crud.update_disbursement(
        db, disbursement_id, payload, current_user
    )
    changes = activity.diff(before, activity.snapshot(updated_item))
    if changes:
        activity.log(db, current_user.username, "update", f"Edited {activity.describe(updated_item)}",
                     "disbursement", updated_item.id, {"changes": changes})
        db.commit()
        db.refresh(updated_item)
    crud.with_person_names(db, [updated_item])
    return updated_item


@app.delete(
    "/disbursements/{disbursement_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["Disbursements"],
)
def delete_disbursement_log(
    disbursement_id: int,
    reason: Optional[str] = None,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(security.get_current_superuser),
):
    """Archives an entry (admins only). It disappears from projects and totals but is
    never erased, and can be restored from the Activity Log."""
    d = db.get(models.Disbursement, disbursement_id, with_for_update=True)
    if not d:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Disbursement log not found",
        )
    if d.is_archived:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This entry is already archived.",
        )

    activity.archive_disbursement(d, current_user)
    activity.log(db, current_user.username, "archive", f"Archived {activity.describe(d)}",
                 "disbursement", d.id, {"entry": activity.snapshot(d), "reason": reason})
    db.commit()
    return None


@app.get(
    "/projects/{project_title}/summary",
    response_model=schemas.ProjectSummaryResponse,
    tags=["Disbursements"],
)
def read_project_summary(
    project_title: str,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(security.get_current_user),
):
    """Returns project summary with totals and encoder logs."""
    summary = crud.get_project_summary(db, project_title)
    if not summary:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )
    return summary