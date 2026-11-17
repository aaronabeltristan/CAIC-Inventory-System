"""Diagnostics: /health, /health/details (Settings → Status) and error IDs for server errors."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import activity
import auth
import models
from tests.conftest import logs, set_setting


def component(client, admin, key):
    data = client.get("/health/details", headers=admin.auth).json()
    return next(c for c in data["components"] if c["key"] == key)


def test_health_needs_no_sign_in(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_details_are_for_admins_only(client, admin, staff):
    assert client.get("/health/details").status_code == 401
    assert client.get("/health/details", headers=staff.auth).status_code == 403
    data = client.get("/health/details", headers=admin.auth).json()
    assert {c["key"] for c in data["components"]} >= {"database", "backups", "cleanup", "errors", "live", "assistant"}
    assert data["status"] in ("ok", "warn", "fail")
    assert "E-DB-01" in data["codes"]


def test_database_check(client, admin):
    db = component(client, admin, "database")
    assert db["status"] in ("ok", "warn") and "ms" in db


def test_backup_check_follows_the_last_result(client, db, admin):
    assert component(client, admin, "backups")["code"] == "W-BK-02"        # none yet
    activity.log(db, "system", "backup", "Backup saved: daily/x.gpg", "backup")
    db.commit()
    assert component(client, admin, "backups")["status"] == "ok"
    activity.log(db, "system", "backup_failed", "Backup FAILED: wrong passphrase", "backup")
    db.commit()
    failed = component(client, admin, "backups")
    assert failed["status"] == "fail" and failed["code"] == "E-BK-01" and "wrong passphrase" in failed["detail"]


def test_old_backup_is_a_warning(client, db, admin):
    activity.log(db, "system", "backup", "Backup saved", "backup")
    db.commit()
    db.query(models.ActivityLog).filter_by(action="backup").update(
        {"created_at": datetime.now(timezone.utc) - timedelta(hours=30)})
    db.commit()
    assert component(client, admin, "backups")["code"] == "W-BK-02"


def test_assistant_check(client, admin):
    # The tests point OLLAMA_HOST at nothing: the model server is "unreachable"
    assert component(client, admin, "assistant")["code"] == "E-AI-01"
    set_setting("chat_access", "off")
    assert component(client, admin, "assistant")["status"] == "off"


def test_cleanup_check_shows_the_last_run(client, admin):
    auth.purge_once()
    cleanup = component(client, admin, "cleanup")
    assert cleanup["status"] == "ok" and "Last run" in cleanup["detail"]


def test_unexpected_errors_get_an_id_and_a_log_row(client, db, admin):
    if not any(getattr(r, "path", "") == "/__test_crash" for r in auth.app.routes):
        @auth.app.get("/__test_crash")
        def crash():
            raise RuntimeError("boom for the test")
    quiet = TestClient(auth.app, raise_server_exceptions=False)
    r = quiet.get("/__test_crash")
    assert r.status_code == 500
    error_id = r.json()["error_id"]
    assert error_id.startswith("E-") and len(error_id) == 10 and error_id in r.json()["detail"]
    row = logs(db, "server_error")[-1]
    assert row.details["error_id"] == error_id and "boom for the test" in row.summary
    errors = component(client, admin, "errors")
    assert errors["code"] == "W-ER-01" and errors["latest_error_id"] == error_id
