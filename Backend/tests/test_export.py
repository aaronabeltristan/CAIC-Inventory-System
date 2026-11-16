"""Excel export of a project and CSV import."""
import io
from decimal import Decimal

from openpyxl import load_workbook

from tests.conftest import add_entry, logs

CSV_HEADER = "project_title,po_number,po_amount,date,cv_no,payee,amount\n"


def import_csv(client, who, text, mode="preview"):
    return client.post(f"/disbursements/import?mode={mode}", headers=who.auth,
                       files={"file": ("entries.csv", io.BytesIO(text.encode("utf-8")), "text/csv")})


# ---------- Export ----------

def test_export_is_a_real_excel_file_with_the_entries(client, db, admin, staff):
    add_entry(client, staff, cv_no="100002", amount="200.00", date="2026-10-02")
    add_entry(client, staff, cv_no="100001", amount="100.50", date="2026-10-01")
    r = client.get("/projects/Bamban Plant/export", headers=staff.auth)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument.spreadsheetml")
    assert ".xlsx" in r.headers["content-disposition"]
    wb = load_workbook(io.BytesIO(r.content))
    assert "Entries" in wb.sheetnames
    cells = [c for row in wb["Entries"].iter_rows(values_only=True) for c in row if c is not None]
    text = " ".join(map(str, cells))
    assert "100001" in text and "100002" in text
    assert text.index("100001") < text.index("100002")  # oldest first
    numbers = [Decimal(str(c)) for c in cells if isinstance(c, (int, float))]
    assert Decimal("100.5") in numbers and Decimal("200") in numbers
    assert len(logs(db, "export")) == 1


def test_export_leaves_out_archived_entries(client, admin, staff):
    add_entry(client, staff, cv_no="200001")
    gone = add_entry(client, staff, cv_no="200002")
    client.delete(f"/disbursements/{gone['id']}", headers=admin.auth)
    wb = load_workbook(io.BytesIO(client.get("/projects/Bamban Plant/export", headers=staff.auth).content))
    text = str(list(wb["Entries"].iter_rows(values_only=True)))
    assert "200001" in text and "200002" not in text


def test_export_unknown_project_is_404(client, staff):
    assert client.get("/projects/Nope/export", headers=staff.auth).status_code == 404


def test_export_needs_sign_in(client, staff):
    add_entry(client, staff)
    assert client.get("/projects/Bamban Plant/export").status_code == 401


def test_export_neutralises_spreadsheet_formulas(client, staff):
    """A payee typed as =HYPERLINK(...) must not become a live formula in Excel."""
    add_entry(client, staff, payee='=HYPERLINK("http://evil.example","click")')
    wb = load_workbook(io.BytesIO(client.get("/projects/Bamban Plant/export", headers=staff.auth).content))
    for row in wb["Entries"].iter_rows():
        for cell in row:
            assert cell.data_type != "f", f"formula in {cell.coordinate}: {cell.value}"


# ---------- Import ----------

def test_import_is_admin_only(client, staff):
    assert import_csv(client, staff, CSV_HEADER).status_code == 403


def test_import_preview_saves_nothing(client, admin):
    r = import_csv(client, admin, CSV_HEADER + "Bamban Plant,5001,50000,2026-10-01,100001,Acme,\"1,500.00\"\n")
    body = r.json()
    assert r.status_code == 200 and body["valid_rows"] == 1 and body["imported"] == 0
    assert body["new_projects"] == ["Bamban Plant"]
    assert client.get("/disbursements/", headers=admin.auth).json() == []


def test_import_commit_saves_valid_rows_and_skips_bad_ones(client, db, admin):
    text = CSV_HEADER + (
        "Bamban Plant,5001,50000,2026-10-01,100001,Acme,1500.00\n"
        "Bamban Plant,5001,50000,10/02/2026,100002,Beta,250\n"
        "Bamban Plant,5001,50000,not-a-date,100003,Gamma,10\n"
        "Bamban Plant,5001,50000,2026-10-03,100004,Delta,-5\n"
        ",5001,50000,2026-10-03,100005,Echo,5\n"
    )
    body = import_csv(client, admin, text, mode="commit").json()
    assert body["imported"] == 2 and body["error_rows"] == 3
    assert {e["line"] for e in body["errors"]} == {4, 5, 6}
    rows = client.get("/disbursements/", headers=admin.auth).json()
    assert sorted(r["cv_no"] for r in rows) == ["100001", "100002"]
    assert len(logs(db, "import")) == 1


def test_import_needs_the_right_columns(client, admin):
    r = import_csv(client, admin, "title,when,amount\nx,2026-01-01,5\n")
    assert r.status_code == 400 and "Missing column" in r.json()["detail"]


def test_import_rejects_empty_file(client, admin):
    assert import_csv(client, admin, "").status_code == 400


# ---------- Import: purchase orders ----------

def test_import_creates_new_projects_with_their_po(client, admin):
    text = CSV_HEADER + (
        "Bamban Plant,20260001,\"250,000.00\",2026-10-01,100001,Acme,1500\n"
        "Bamban Plant,,,2026-10-02,100002,Beta,500\n"          # the PO may be given on one row only
        "Calamba Office,20260002,80000,2026-10-03,100003,Gamma,700\n"
    )
    preview = import_csv(client, admin, text).json()
    assert preview["error_rows"] == 0
    assert {d["title"]: d["po_number"] for d in preview["new_project_details"]} == \
        {"Bamban Plant": "20260001", "Calamba Office": "20260002"}
    assert import_csv(client, admin, text, mode="commit").json()["imported"] == 3
    projects = {p["title"]: p for p in client.get("/projects", headers=admin.auth).json()}
    assert projects["Bamban Plant"]["po_number"] == "20260001"
    assert Decimal(projects["Bamban Plant"]["po_amount"]) == Decimal("250000.00")
    assert projects["Bamban Plant"]["has_po_file"] is False   # the PDF is added afterwards


def test_import_new_project_without_po_is_skipped(client, admin):
    body = import_csv(client, admin, CSV_HEADER + "Bamban Plant,,,2026-10-01,100001,Acme,1500\n").json()
    assert body["valid_rows"] == 0 and "is new" in body["errors"][0]["message"]
    body = import_csv(client, admin, CSV_HEADER + "Bamban Plant,5001,,2026-10-01,100001,Acme,1500\n").json()
    assert body["valid_rows"] == 0 and "po_amount" in body["errors"][0]["message"]


def test_import_rows_of_one_project_must_agree_on_the_po(client, admin):
    text = CSV_HEADER + (
        "Bamban Plant,5001,50000,2026-10-01,100001,Acme,1500\n"
        "Bamban Plant,5002,50000,2026-10-02,100002,Beta,500\n"
        "Calamba Office,5003,80000,2026-10-03,100003,Gamma,700\n"
    )
    body = import_csv(client, admin, text).json()
    assert body["valid_rows"] == 1 and {e["line"] for e in body["errors"]} == {2, 3}
    assert "different po_number" in body["errors"][0]["message"]


def test_import_po_number_belongs_to_one_project(client, admin, staff):
    from tests.conftest import new_project
    new_project(client, staff, "Existing", po_number="5009")
    text = CSV_HEADER + (
        "Bamban Plant,5009,50000,2026-10-01,100001,Acme,1500\n"      # already Existing's PO
        "Calamba Office,5005,80000,2026-10-03,100002,Gamma,700\n"
        "Laguna Depot,5005,90000,2026-10-04,100003,Delta,800\n"      # same PO as Calamba Office
    )
    body = import_csv(client, admin, text).json()
    assert body["valid_rows"] == 0
    messages = " ".join(e["message"] for e in body["errors"])
    assert "already belongs to project 'Existing'" in messages and "also given for project" in messages


def test_import_into_an_existing_project_may_repeat_or_leave_out_its_po(client, admin, staff):
    from tests.conftest import new_project
    new_project(client, staff, "Bamban Plant", po_number="5001", amount="50000")
    text = CSV_HEADER + (
        "Bamban Plant,5001,50000,2026-10-01,100001,Acme,1500\n"
        "Bamban Plant,,,2026-10-02,100002,Beta,500\n"
    )
    assert import_csv(client, admin, text).json()["valid_rows"] == 2
    wrong = import_csv(client, admin, CSV_HEADER + "Bamban Plant,5077,50000,2026-10-01,100009,Acme,1\n").json()
    assert wrong["valid_rows"] == 0 and "already has PO 5001" in wrong["errors"][0]["message"]


def test_import_adds_the_po_of_an_older_project(client, admin, staff):
    add_entry(client, staff, project="Old Project")   # made without a PO
    text = CSV_HEADER + "Old Project,5900,12000,2026-10-05,100008,Acme,100\n"
    body = import_csv(client, admin, text, mode="commit").json()
    assert body["imported"] == 1 and body["pos_added"] == 1
    p = client.get("/projects", headers=admin.auth).json()[0]
    assert p["po_number"] == "5900"


def test_export_can_be_imported_again(client, admin, staff):
    """The Entries sheet starts with the import columns, PO included."""
    from tests.conftest import new_project
    new_project(client, staff, "Bamban Plant", po_number="5001", amount="50000")
    add_entry(client, staff)
    wb = load_workbook(io.BytesIO(client.get("/projects/Bamban Plant/export", headers=staff.auth).content))
    header = [c for c in next(wb["Entries"].iter_rows(values_only=True))]
    assert header[:7] == ["project_title", "po_number", "po_amount", "date", "cv_no", "payee", "amount"]
    first = next(wb["Entries"].iter_rows(min_row=2, values_only=True))
    assert first[1] == "5001" and Decimal(str(first[2])) == Decimal("50000")


def test_import_po_and_cv_must_be_numbers_only(client, admin):
    text = CSV_HEADER + (
        "Bamban Plant,PO-1,50000,2026-10-01,100001,Acme,1500\n"
        "Calamba Office,5002,80000,2026-10-03,CV-3,Gamma,700\n"
        "Laguna Depot,5003,90000,2026-10-04,000004,Delta,800\n"
    )
    body = import_csv(client, admin, text).json()
    assert body["valid_rows"] == 1
    messages = {e["line"]: e["message"] for e in body["errors"]}
    assert "po_number 'PO-1' must be numbers only" in messages[2]
    assert "cv_no 'CV-3' must be numbers only" in messages[3]
