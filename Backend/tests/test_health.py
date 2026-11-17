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


def test_schema_upgrade_only_changes_what_is_missing(capsys):
    """Every backend process runs ensure_schema() at start. When nothing is missing it must
    change nothing (no table locks): several processes doing ALTER TABLE at once, while screens
    were reading, caused a deadlock (Server error E-01C64378, found by a fault test)."""
    import database
    from sqlalchemy import text
    database.ensure_schema()
    capsys.readouterr()
    database.ensure_schema()
    assert "[schema]" not in capsys.readouterr().out          # nothing to do the second time
    with database.engine.begin() as conn:
        conn.execute(text("DROP INDEX IF EXISTS ux_projects_po_number"))
    database.ensure_schema()
    assert "applied 1 database change" in capsys.readouterr().out
    with database.engine.connect() as conn:
        assert conn.execute(text("SELECT 1 FROM pg_indexes WHERE indexname = 'ux_projects_po_number'")).first()


def _beat(db, state, minutes_ago):
    row = db.get(models.AppSetting, "backup_heartbeat")
    when = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    if row is None:
        db.add(models.AppSetting(key="backup_heartbeat", value=state, updated_by="backup", updated_at=when))
    else:
        row.value, row.updated_at = state, when
    db.commit()


def test_stopped_backup_service_is_noticed_from_its_heartbeat(client, db, admin):
    activity.log(db, "system", "backup", "Backup saved", "backup")
    db.commit()
    _beat(db, "idle", 1)
    ok = component(client, admin, "backups")
    assert ok["status"] == "ok" and "service alive" in ok["detail"]
    _beat(db, "idle", 4)                                    # no "alive" for 4 minutes
    stopped = component(client, admin, "backups")
    assert stopped["status"] == "warn" and stopped["code"] == "W-BK-04"


def test_a_long_backup_is_not_mistaken_for_a_stopped_service(client, db, admin):
    activity.log(db, "system", "backup", "Backup saved", "backup")
    db.commit()
    db.query(models.ActivityLog).filter_by(action="backup").update(
        {"created_at": datetime.now(timezone.utc) - timedelta(hours=3)})   # the previous backup, earlier today
    db.commit()
    _beat(db, "busy", 20)                                   # a backup has been running 20 minutes
    assert component(client, admin, "backups")["status"] == "ok"
    _beat(db, "busy", 70)                                   # far too long: something's wrong
    assert component(client, admin, "backups")["code"] == "W-BK-04"


def test_without_any_heartbeat_the_check_is_skipped(client, db, admin):
    """An older backup container (before heartbeats) mustn't raise a false alarm."""
    activity.log(db, "system", "backup", "Backup saved", "backup")
    db.commit()
    assert component(client, admin, "backups")["status"] == "ok"


def test_service_stopped_right_after_a_backup_is_still_noticed(client, db, admin):
    """Found in fault test F3: the service said "busy", made the backup, and was stopped before
    saying "idle" again, so Status allowed it 60 minutes instead of 3."""
    _beat(db, "busy", 5)                                    # "busy" 5 minutes ago...
    activity.log(db, "system", "backup", "Backup saved: manual/x.gpg", "backup")
    db.commit()
    db.query(models.ActivityLog).filter_by(action="backup").update(
        {"created_at": datetime.now(timezone.utc) - timedelta(minutes=4)})   # ...finished 4 minutes ago
    db.commit()
    assert component(client, admin, "backups")["code"] == "W-BK-04"
