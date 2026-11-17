"""The CSV files in sample_data/ import the way the README says.

test.ps1 mounts the project's sample_data folder and sets SAMPLE_DATA_DIR; without it (e.g.
pytest run by hand) these tests are skipped.
"""
import io
import os
from pathlib import Path

import pytest

SAMPLE_DIR = os.environ.get("SAMPLE_DATA_DIR") or str(Path(__file__).resolve().parents[2] / "sample_data")
SAMPLE = Path(SAMPLE_DIR) / "disbursements_import_sample.csv"
ERRORS = Path(SAMPLE_DIR) / "disbursements_import_errors.csv"
PO_PDF = Path(SAMPLE_DIR) / "PO_20250001_sample.pdf"

pytestmark = pytest.mark.skipif(not SAMPLE.exists(), reason="sample_data folder not available")


def import_file(client, who, path, mode="preview"):
    return client.post(f"/disbursements/import?mode={mode}", headers=who.auth,
                       files={"file": (path.name, io.BytesIO(path.read_bytes()), "text/csv")})


def test_sample_imports_completely(client, admin):
    preview = import_file(client, admin, SAMPLE)
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert (body["valid_rows"], body["error_rows"]) == (829, 0), body["errors"][:5]
    assert len(body["new_projects"]) == 100
    assert all(d["po_number"].isdigit() for d in body["new_project_details"])
    assert import_file(client, admin, SAMPLE, mode="commit").json()["imported"] == 829
    projects = client.get("/projects", headers=admin.auth).json()
    assert len(projects) == 100 and all(p["po_number"] for p in projects)


def test_errors_file_reports_each_problem(client, admin):
    body = import_file(client, admin, ERRORS).json()
    assert (body["valid_rows"], body["error_rows"]) == (4, 17), body["errors"]
    text = " ".join(e["message"] for e in body["errors"])
    for expected in ("isn't a valid date", "greater than zero", "isn't a number", "is repeated",
                     "project_title is empty", "payee is empty", "cv_no is empty", "more than 2 decimal",
                     "too large", "is new", "different po_number", "also given for project",
                     "must be numbers only", "po_amount 'zero' isn't a number"):
        assert expected in text, expected


def test_errors_file_after_the_sample_finds_the_existing_cv(client, admin):
    import_file(client, admin, SAMPLE, mode="commit")
    body = import_file(client, admin, ERRORS).json()
    assert body["valid_rows"] == 3
    assert any("250001 already exists" in e["message"] for e in body["errors"])


def test_sample_po_pdf_attaches_to_its_project(client, admin):
    if not PO_PDF.exists():
        pytest.skip("no sample PO PDF")
    import_file(client, admin, SAMPLE, mode="commit")
    project = next(p for p in client.get("/projects", headers=admin.auth).json() if p["po_number"] == "20250001")
    r = client.put(f"/projects/{project['id']}/po", headers=admin.auth,
                   files={"po_file": (PO_PDF.name, PO_PDF.read_bytes(), "application/pdf")})
    assert r.status_code == 200 and r.json()["has_po_file"] is True
