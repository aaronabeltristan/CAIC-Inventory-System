"""Projects and their purchase orders: required fields, unique PO numbers, the PDF."""
from decimal import Decimal

import models
from tests.conftest import PDF, add_entry, logs, new_project


def test_new_project_needs_title_po_number_amount_and_pdf(client, staff):
    r = new_project(client, staff, "Calamba Warehouse", po_number="202600001", amount="250,000.50")
    assert r.status_code == 201, r.text
    p = r.json()
    assert (p["po_number"], Decimal(p["po_amount"]), p["has_po_file"]) == ("202600001", Decimal("250000.50"), True)
    listed = client.get("/projects", headers=staff.auth).json()[0]
    assert listed["po_number"] == "202600001" and listed["has_po_file"] is True


def test_each_part_of_the_po_is_required(client, staff):
    assert new_project(client, staff, "A", po_number="  ").status_code == 422
    assert new_project(client, staff, "B", amount="0").status_code == 422
    assert new_project(client, staff, "C", amount="abc").status_code == 422
    assert new_project(client, staff, "D", amount="10.555").status_code == 422
    assert new_project(client, staff, "E", pdf=None).status_code == 422
    assert client.get("/projects", headers=staff.auth).json() == []


def test_po_file_must_be_a_pdf(client, staff):
    r = new_project(client, staff, "A", pdf=b"\x89PNG\r\n\x1a\n not a pdf", filename="po.pdf")
    assert r.status_code == 400 and "PDF" in r.json()["detail"]


def test_po_number_is_unique_ignoring_capitals_and_spaces(client, staff, admin):
    assert new_project(client, staff, "First", po_number="5100").status_code == 201
    r = new_project(client, staff, "Second", po_number=" 5100 ")
    assert r.status_code == 409 and "First" in r.json()["detail"]
    # still taken after the first project is archived
    pid = client.get("/projects", headers=admin.auth).json()[0]["id"]
    client.delete(f"/projects/{pid}", headers=admin.auth)
    r = new_project(client, staff, "Third", po_number="5100")
    assert r.status_code == 409 and "archived" in r.json()["detail"]


def test_anyone_signed_in_can_view_the_pdf(client, staff, staff2):
    pid = new_project(client, staff, "Calamba Warehouse").json()["id"]
    r = client.get(f"/projects/{pid}/po-file", headers=staff2.auth)
    assert r.status_code == 200 and r.content == PDF
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"].startswith("inline")
    assert client.get(f"/projects/{pid}/po-file").status_code == 401


def test_po_link_opens_the_pdf_without_the_sign_in_header(client, staff):
    first = new_project(client, staff, "First").json()["id"]
    second = new_project(client, staff, "Second").json()["id"]
    link = client.post(f"/projects/{first}/po-file/link", headers=staff.auth).json()["url"]
    r = client.get(link)                                  # a new browser tab: no header
    assert r.status_code == 200 and r.content == PDF
    other = link.replace(f"/projects/{first}/", f"/projects/{second}/")
    assert client.get(other).status_code == 401           # only for that project's PO
    assert client.get(f"/projects/{first}/po-file?t=forged").status_code == 401


def test_summary_shows_the_po(client, staff):
    new_project(client, staff, "Calamba Warehouse", po_number="5007", amount="5000")
    s = client.get("/projects/Calamba Warehouse/summary", headers=staff.auth).json()
    assert (s["po_number"], Decimal(s["po_amount"]), s["has_po_file"]) == ("5007", Decimal("5000.00"), True)


def test_admin_adds_a_po_to_an_older_project(client, db, admin, staff):
    add_entry(client, staff)  # made through an entry, like projects made before POs: no PO
    pid = client.get("/projects", headers=admin.auth).json()[0]["id"]
    assert client.get("/projects", headers=admin.auth).json()[0]["po_number"] is None
    url = f"/projects/{pid}/po"
    assert client.put(url, headers=staff.auth, data={"po_number": "5009"}).status_code == 403
    # all three are needed the first time
    assert client.put(url, headers=admin.auth, data={"po_number": "5009"}).status_code == 422
    r = client.put(url, headers=admin.auth, data={"po_number": "5009", "amount": "1000"},
                   files={"po_file": ("po.pdf", PDF, "application/pdf")})
    assert r.status_code == 200 and r.json()["has_po_file"] is True
    # later, one part can be changed on its own
    r = client.put(url, headers=admin.auth, data={"amount": "1200"})
    assert Decimal(r.json()["po_amount"]) == Decimal("1200.00")
    assert len(logs(db, "project_po_update")) == 2


def test_po_cant_be_changed_to_one_already_used(client, admin, staff):
    new_project(client, staff, "First", po_number="5001")
    pid = new_project(client, staff, "Second", po_number="5002").json()["id"]
    assert client.put(f"/projects/{pid}/po", headers=admin.auth, data={"po_number": "5001"}).status_code == 409


def test_creating_a_project_is_logged_with_its_po(client, db, staff):
    new_project(client, staff, "Calamba Warehouse", po_number="5055")
    entry = logs(db, "project_create")[-1]
    assert "5055" in entry.summary and entry.details["po_file"] == "po.pdf"


def test_permanently_deleting_a_project_removes_its_pdf(client, db, admin, staff):
    pid = new_project(client, staff, "Calamba Warehouse").json()["id"]
    client.delete(f"/projects/{pid}", headers=admin.auth)
    r = client.post("/archived/delete", headers=admin.auth, json={"projects": [pid], "entries": []})
    assert r.status_code == 200, r.text
    db.expire_all()
    assert db.get(models.ProjectPoFile, pid) is None


def test_po_number_must_be_numbers_only(client, admin, staff):
    for po in ("PO-2026-001", "2026 001", "12a", "1" * 31):
        r = new_project(client, staff, f"Project {po}", po_number=po)
        assert r.status_code == 422 and "numbers only" in r.json()["detail"], po
    pid = new_project(client, staff, "Good", po_number="000777").json()["id"]
    assert client.get("/projects", headers=staff.auth).json()[0]["po_number"] == "000777"
    assert client.put(f"/projects/{pid}/po", headers=admin.auth, data={"po_number": "PO-1"}).status_code == 422
