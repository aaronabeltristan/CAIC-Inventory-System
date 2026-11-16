"""Settings (user menu → Settings): saving, who can read what, and that each setting takes effect."""
from datetime import datetime, timedelta, timezone

import jwt

import models
import security
from tests.conftest import PASSWORD, add_entry, login, logs, make_user, set_setting


def put(client, who, **changes):
    return client.put("/settings", headers=who.auth, json=changes)


# ---------- Reading and saving ----------

def test_public_settings_need_no_sign_in_and_hide_the_rest(client):
    data = client.get("/settings/public").json()
    assert data["company_name"] == "Calamba Allied Industrial Corporation"
    assert data["allow_registration"] is True and data["currency"] == "PHP"
    assert "archive_retention_days" not in data and "login_max_failures" not in data


def test_admins_get_the_descriptions_and_can_save_several_at_once(client, db, admin):
    r = client.get("/settings", headers=admin.auth).json()
    keys = {s["key"] for s in r["schema"]}
    assert {"login_max_failures", "chat_access", "company_name", "currency"} <= keys
    r = put(client, admin, login_max_failures=7, company_name="  Acme   Corp ")
    assert r.status_code == 200
    assert r.json()["values"]["login_max_failures"] == 7
    assert r.json()["values"]["company_name"] == "Acme Corp"
    assert len(logs(db, "settings_update")) == 1


def test_one_bad_value_saves_nothing(client, admin):
    r = put(client, admin, login_max_failures=7, chat_access="sometimes")
    assert r.status_code == 422
    assert client.get("/settings", headers=admin.auth).json()["values"]["login_max_failures"] == 5


def test_unknown_settings_and_bad_lists_are_refused(client, admin):
    assert put(client, admin, jwt_secret_key="x").status_code == 422
    assert put(client, admin, single_session_exempt="ok, not ok!").status_code == 422
    assert put(client, admin, activity_log_keep_days=10).status_code == 422   # 0, or at least 30


# ---------- Security ----------

def test_lockout_follows_the_settings(client):
    make_user("clerk")
    set_setting("login_max_failures", 3)
    for _ in range(3):
        assert client.post("/token", data={"username": "clerk", "password": "wrong"}).status_code == 401
    assert client.post("/token", data={"username": "clerk", "password": PASSWORD}).status_code == 429


def test_idle_sign_out_sets_how_long_a_sign_in_lasts(client):
    make_user("clerk")
    set_setting("session_idle_minutes", 60)
    token = login(client, "clerk")["Authorization"].split()[1]
    claims = jwt.decode(token, security.SECRET_KEY, algorithms=[security.ALGORITHM])
    assert 59 * 60 <= claims["exp"] - claims["iat"] <= 61 * 60


def test_single_session_can_be_turned_off(client):
    make_user("clerk")
    set_setting("single_session", False)
    a, b = login(client, "clerk"), login(client, "clerk")  # no "waiting for the other device"
    assert client.get("/projects", headers=a).status_code == 200
    assert client.get("/projects", headers=b).status_code == 200


def test_create_account_can_be_turned_off(client):
    set_setting("allow_registration", False)
    r = client.post("/auth/register", json={"username": "newbie", "name": "New Person", "password": "Long-enough-1"})
    assert r.status_code == 403


def test_forgot_password_can_be_turned_off(client):
    set_setting("allow_password_reset", False)
    assert client.post("/auth/reset-request", json={"username": "clerk"}).status_code == 403


# ---------- Requests ----------

def test_edit_reason_can_be_required(client, staff):
    e = add_entry(client, staff)
    set_setting("edit_reason_required", True)
    body = {"payee": "Other", "editor_name": "Staff"}
    assert client.post(f"/disbursements/{e['id']}/edit-request", headers=staff.auth, json=body).status_code == 400
    body["reason"] = "Typo in payee"
    assert client.post(f"/disbursements/{e['id']}/edit-request", headers=staff.auth, json=body).status_code == 201


def test_staff_edits_apply_straight_away_when_approval_is_off(client, db, staff):
    e = add_entry(client, staff)
    set_setting("staff_edits_need_approval", False)
    r = client.post(f"/disbursements/{e['id']}/edit-request", headers=staff.auth,
                    json={"payee": "Fixed Payee", "editor_name": "Staff"})
    assert r.status_code == 201 and r.json()["status"] == "approved"
    db.expire_all()
    assert db.get(models.Disbursement, e["id"]).payee == "Fixed Payee"
    assert logs(db, "update")


# ---------- System ----------

def test_chat_assistant_can_be_limited_to_admins_or_turned_off(client, admin, staff):
    set_setting("chat_access", "admins")
    msg = {"messages": [{"role": "user", "content": "hi"}]}
    assert client.post("/chat", headers=staff.auth, json=msg).status_code == 403
    set_setting("chat_access", "off")
    assert client.post("/chat", headers=admin.auth, json=msg).status_code == 403


def test_routine_activity_log_rows_are_cleaned_up_but_changes_kept(client, db, admin, staff):
    import activity
    add_entry(client, staff)
    old = datetime.now(timezone.utc) - timedelta(days=40)
    db.query(models.ActivityLog).update({"created_at": old})
    db.commit()
    assert activity.purge_routine_logs(db) == 0          # 0 = keep everything (default)
    set_setting("activity_log_keep_days", 30)
    assert activity.purge_routine_logs(db) >= 1          # the old sign-ins
    db.expire_all()
    assert not logs(db, "login") and logs(db, "create")  # the added entry is kept


def test_backup_now_is_recorded_once(client, db, admin):
    assert client.post("/settings/backup-now", headers=admin.auth).status_code == 202
    assert client.post("/settings/backup-now", headers=admin.auth).status_code == 202
    assert len(logs(db, "backup_requested")) == 1
    assert client.get("/settings", headers=admin.auth).json()["backup_requested"] is True


# ---------- Company ----------

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def test_logo_upload_and_reset(client, admin, staff):
    assert client.get("/settings/logo").status_code == 404
    files = {"file": ("logo.png", PNG, "image/png")}
    assert client.put("/settings/logo", headers=staff.auth, files=files).status_code == 403
    r = client.put("/settings/logo", headers=admin.auth, files=files)
    assert r.status_code == 200 and r.json()["values"]["logo_version"]
    assert client.get("/settings/logo").content == PNG
    bad = {"file": ("logo.txt", b"not a picture", "text/plain")}
    assert client.put("/settings/logo", headers=admin.auth, files=bad).status_code == 400
    assert client.delete("/settings/logo", headers=admin.auth).status_code == 200
    assert client.get("/settings/logo").status_code == 404


def test_currency_shows_in_new_activity_log_summaries(client, db, admin, staff):
    set_setting("currency", "USD")
    e = add_entry(client, staff)
    client.delete(f"/disbursements/{e['id']}", headers=admin.auth)
    assert "USD 1,500.00" in logs(db, "archive")[-1].summary
