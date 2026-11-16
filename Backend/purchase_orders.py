"""Projects and their purchase orders (PO).

A new project needs a title, a PO number, the PO amount and a PDF copy of the PO. The PO
number is numbers only (0-9, leading zeros kept) and identifies the project: no two projects
(archived ones included) may have the same one. Projects made before POs existed have none; an
admin can add or correct a project's PO later (PUT /projects/{id}/po).

- POST /projects                 anyone signed in: create a project (form with the PDF)
- PUT  /projects/{id}/po         admins: set or change the PO number, amount and/or PDF
- POST /projects/{id}/po-file/link  anyone signed in: a link to the PDF that works for 2 minutes
- GET  /projects/{id}/po-file    the PDF, shown in the browser: with the sign-in header, or with
                                 ?t= from the link above (a new browser tab can't send the header)
"""
import re
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

import jwt
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile, status
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import activity
import app_settings
import crud
import database
import models
import schemas
import security

router = APIRouter(tags=["Projects"])

MAX_PO_FILE_BYTES = 10 * 1024 * 1024  # 10 MB
PO_NUMBER_RULE = re.compile(r"^\d{1,30}$")  # numbers only
MAX_AMOUNT = Decimal("999999999999.99")


# ---------- Checks ----------

def clean_title(value: str) -> str:
    title = " ".join((value or "").split())
    if not title:
        raise HTTPException(422, "Enter a project title.")
    if len(title) > 255:
        raise HTTPException(422, "The project title is too long (255 characters at most).")
    return title


def clean_po_number(value: str) -> str:
    po = (value or "").strip()
    if not po:
        raise HTTPException(422, "Enter the PO number.")
    if not PO_NUMBER_RULE.match(po):
        raise HTTPException(422, "The PO number must be numbers only (0-9), up to 30 digits.")
    return po


def clean_amount(value: str) -> Decimal:
    text = (value or "").replace(",", "").strip()
    try:
        amount = Decimal(text)
    except InvalidOperation:
        raise HTTPException(422, "Enter the PO amount as a number.")
    if not amount.is_finite() or amount <= 0:
        raise HTTPException(422, "The PO amount must be greater than zero.")
    if amount > MAX_AMOUNT:
        raise HTTPException(422, "The PO amount is too large.")
    if amount != amount.quantize(Decimal("0.01")):
        raise HTTPException(422, "The PO amount can have at most two decimals.")
    return amount.quantize(Decimal("0.01"))


def read_pdf(file: UploadFile) -> bytes:
    data = file.file.read(MAX_PO_FILE_BYTES + 1)
    if len(data) > MAX_PO_FILE_BYTES:
        raise HTTPException(413, "The PO file is too large (10 MB at most).")
    if not data.startswith(b"%PDF-"):  # the real type, whatever the name or browser says
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The PO file must be a PDF.")
    return data


def _file_name(file: UploadFile) -> str:
    name = (file.filename or "").replace("\\", "/").split("/")[-1].strip() or "purchase-order.pdf"
    return name[:255]


def check_po_unused(db: Session, po_number: str, except_project_id: Optional[int] = None) -> None:
    query = db.query(models.Project).filter(func.lower(models.Project.po_number) == po_number.lower())
    if except_project_id is not None:
        query = query.filter(models.Project.id != except_project_id)
    other = query.first()
    if other:
        where = " (archived)" if other.is_archived else ""
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"PO number {po_number} is already used by project '{other.title}'{where}.",
        )


def _set_file(project: models.Project, data: bytes, file: UploadFile, user: models.User) -> None:
    if project.po_file is None:
        project.po_file = models.ProjectPoFile()
    project.po_file.content = data
    project.po_file.filename = _file_name(file)
    project.po_file.size = len(data)
    project.po_file.uploaded_by = user.username
    project.po_file.uploaded_at = datetime.now(timezone.utc)


def _response(project: models.Project) -> dict:
    return {
        "id": project.id,
        "title": project.title,
        "total_amount": project.total_amount,
        "entry_count": project.entry_count,
        "po_number": project.po_number,
        "po_amount": project.po_amount,
        "has_po_file": project.po_file is not None,
    }


def _commit(db: Session, po_number: str) -> None:
    try:
        db.commit()
    except IntegrityError:  # someone saved the same PO number at the same moment
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, f"PO number {po_number} is already used by another project.")


# ---------- Routes ----------

@router.post("/projects", response_model=schemas.ProjectResponse, status_code=status.HTTP_201_CREATED)
def create_project(
    title: str = Form(...),
    po_number: str = Form(...),
    amount: str = Form(...),
    po_file: UploadFile = File(...),
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(security.get_current_user),
):
    """Creates a project. Needs the title, PO number, PO amount and the PO as a PDF."""
    title = clean_title(title)
    po_number = clean_po_number(po_number)
    po_amount = clean_amount(amount)
    data = read_pdf(po_file)

    existing = crud.get_project_by_title(db, title=title)
    if existing and existing.is_archived:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "A project with this title is archived. An admin can restore it from Activity Log → Archived.")
    if existing:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Project with this title already exists")
    check_po_unused(db, po_number)

    project = models.Project(title=title, po_number=po_number, po_amount=po_amount)
    _set_file(project, data, po_file, current_user)
    db.add(project)
    db.flush()
    activity.log(db, current_user.username, "project_create",
                 f"Created project '{title}' (PO {po_number}, {app_settings.money(po_amount)})",
                 "project", project.id,
                 {"title": title, "po_number": po_number, "po_amount": activity.plain(po_amount),
                  "po_file": project.po_file.filename})
    _commit(db, po_number)
    db.refresh(project)
    return _response(project)


@router.put("/projects/{project_id}/po", response_model=schemas.ProjectResponse)
def update_po(
    project_id: int,
    po_number: Optional[str] = Form(None),
    amount: Optional[str] = Form(None),
    po_file: Optional[UploadFile] = File(None),
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(security.get_current_superuser),
):
    """Admins: adds or corrects a project's PO. Send only what changes. A project ends up
    with all three (number, amount, PDF) once it has a PO."""
    project = db.get(models.Project, project_id, with_for_update=True)
    if not project:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")

    changes = {}
    if po_number is not None:
        new = clean_po_number(po_number)
        if new != project.po_number:
            check_po_unused(db, new, except_project_id=project.id)
            changes["po_number"] = [project.po_number, new]
            project.po_number = new
    if amount is not None:
        new_amount = clean_amount(amount)
        if new_amount != project.po_amount:
            changes["po_amount"] = [activity.plain(project.po_amount), activity.plain(new_amount)]
            project.po_amount = new_amount
    if po_file is not None and (po_file.filename or "").strip():
        data = read_pdf(po_file)
        old_name = project.po_file.filename if project.po_file else None
        _set_file(project, data, po_file, current_user)
        changes["po_file"] = [old_name, project.po_file.filename]

    missing = [label for label, ok in (("PO number", project.po_number), ("PO amount", project.po_amount),
                                       ("PO file", project.po_file is not None)) if not ok]
    if missing:
        raise HTTPException(422,
                            f"Add the {', '.join(missing)} too: a PO needs its number, amount and PDF.")
    if changes:
        activity.log(db, current_user.username, "project_po_update",
                     f"Updated the PO of project '{project.title}' (PO {project.po_number})",
                     "project", project.id, {"changes": changes})
        _commit(db, project.po_number)
        db.refresh(project)
    return _response(project)


LINK_SECONDS = 120


@router.post("/projects/{project_id}/po-file/link")
def po_file_link(
    project_id: int,
    db: Session = Depends(database.get_db),
    current_user: models.User = Depends(security.get_current_user),
):
    """A link to open the PO in a new browser tab. It only works for this project's PO, for
    LINK_SECONDS, and only while this account is still allowed in."""
    if db.get(models.ProjectPoFile, project_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This project has no PO file.")
    now = int(time.time())
    token = jwt.encode({"sub": current_user.username, "pid": project_id, "use": "po-file",
                        "iat": now, "exp": now + LINK_SECONDS}, security.SECRET_KEY, algorithm=security.ALGORITHM)
    return {"url": f"/projects/{project_id}/po-file?t={token}", "expires_in": LINK_SECONDS}


def _viewer(request: Request, project_id: int, t: Optional[str], db: Session) -> models.User:
    """Who is asking: from the ?t= link, or the usual sign-in header."""
    if t:
        try:
            claims = jwt.decode(t, security.SECRET_KEY, algorithms=[security.ALGORITHM])
        except jwt.PyJWTError:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "This link has expired. Open the PO again from the app.")
        if claims.get("use") != "po-file" or claims.get("pid") != project_id:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "This link isn't for this PO.")
        user = db.query(models.User).filter(models.User.username == claims.get("sub")).first()
        if not user or not user.is_active or not user.is_approved:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "This account can't open files any more.")
        return user
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated", headers={"WWW-Authenticate": "Bearer"})
    user = security.get_current_user_allow_temporary(token=header.split(" ", 1)[1], db=db)
    return security.get_current_user(user=user)


@router.get("/projects/{project_id}/po-file")
def get_po_file(
    project_id: int,
    request: Request,
    t: Optional[str] = None,
    db: Session = Depends(database.get_db),
):
    """The PO as a PDF, shown in the browser (not downloaded)."""
    _viewer(request, project_id, t, db)
    row = db.get(models.ProjectPoFile, project_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This project has no PO file.")
    safe = re.sub(r'[^A-Za-z0-9._ -]', "_", row.filename) or "purchase-order.pdf"
    return Response(row.content, media_type="application/pdf", headers={
        "Content-Disposition": f'inline; filename="{safe}"',
        "Cache-Control": "private, no-store",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    })
