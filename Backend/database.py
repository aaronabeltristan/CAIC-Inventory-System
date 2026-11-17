import os
import re
from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker


def _load_env_file() -> None:
    """Reads Backend/.env (KEY=value lines) when the backend is run by hand on a laptop.
    Real environment variables win. In Docker the values come from docker-compose.yml instead,
    and .env is never copied into the image."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip().strip('"').strip("'")
            if value:
                os.environ.setdefault(key.strip(), value)


_load_env_file()

# The database address and password are never written in the code: they come from
# Backend/.env (see .env.example) or, in Docker, from docker-compose.yml.
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. Put it in Backend/.env (see Backend/.env.example), e.g. "
        "DATABASE_URL=postgresql+psycopg://postgres:<password>@database.nyala-ordinal.ts.net:5432/caic_db_test"
    )


def ensure_database_exists(db_url: str):
    """Connects to the default 'postgres' database to ensure target database exists."""
    base_url, db_name = db_url.rsplit("/", 1)
    db_name = db_name.split("?")[0]  # Strip any query parameters if present

    if db_name == "postgres":
        return

    # Connect to system 'postgres' DB with AUTOCOMMIT (required for CREATE DATABASE)
    admin_url = f"{base_url}/postgres"
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")

    with admin_engine.connect() as conn:
        result = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :dbname"),
            {"dbname": db_name}
        )
        if not result.scalar():
            conn.execute(text(f'CREATE DATABASE "{db_name}"'))
            print(f"Database '{db_name}' created successfully on remote server.")


# 1. Ensure target DB exists before creating the main connection pool
ensure_database_exists(DATABASE_URL)

# 2. Main Database Engine
engine = create_engine(
    DATABASE_URL,
    # Connections per backend process (set in docker-compose.yml). Default up to 40: matches the
    # 40 requests a process works on at once. Processes x pool must stay under PostgreSQL's
    # max_connections (200 in docker-compose.yml).
    pool_size=int(os.getenv("DB_POOL_SIZE", "20")),
    max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "20")),
    pool_pre_ping=True,
    connect_args={"connect_timeout": 5}
)

SessionLocal = sessionmaker(autoflush=False, autocommit=False, bind=engine)
Base = declarative_base()


def ensure_schema():
    """Adds columns that create_all() can't add to tables that already exist.

    Safe to run every startup: each statement does nothing if the column is already there.
    Older copies of the backend keep working, since they simply ignore the new columns.
    """
    if engine.dialect.name != "postgresql":
        return
    statements = [
        "ALTER TABLE disbursements ADD COLUMN IF NOT EXISTS is_archived BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE disbursements ADD COLUMN IF NOT EXISTS archived_by VARCHAR",
        "ALTER TABLE disbursements ADD COLUMN IF NOT EXISTS archived_at TIMESTAMP WITH TIME ZONE",
        "CREATE INDEX IF NOT EXISTS ix_disbursements_is_archived ON disbursements (is_archived)",
        "ALTER TABLE deletion_requests ADD COLUMN IF NOT EXISTS request_type VARCHAR(20) NOT NULL DEFAULT 'archive'",
        "ALTER TABLE deletion_requests ADD COLUMN IF NOT EXISTS changes JSON",
        "CREATE INDEX IF NOT EXISTS ix_deletion_requests_request_type ON deletion_requests (request_type)",
        "ALTER TABLE disbursements ADD COLUMN IF NOT EXISTS archived_with_project BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS is_archived BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS archived_by VARCHAR",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS archived_at TIMESTAMP WITH TIME ZONE",
        "CREATE INDEX IF NOT EXISTS ix_projects_is_archived ON projects (is_archived)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT FALSE",
        # Existing requests count as already seen; new answers start unseen
        "ALTER TABLE deletion_requests ADD COLUMN IF NOT EXISTS requester_seen BOOLEAN NOT NULL DEFAULT TRUE",
        # Accounts that already exist stay approved
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_approved BOOLEAN NOT NULL DEFAULT TRUE",
        "CREATE INDEX IF NOT EXISTS ix_users_is_approved ON users (is_approved)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS token_version INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS session_id VARCHAR(64)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS session_seen_at TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_active_at TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE login_challenges ADD COLUMN IF NOT EXISTS polled_at TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE deletion_requests ALTER COLUMN requester_seen SET DEFAULT FALSE",
        # Purchase orders: a PO number (unique, ignoring capitals), the PO amount and a PDF copy
        # (table project_po_files). Projects made before this have none until an admin adds them.
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS po_number VARCHAR(60)",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS po_amount NUMERIC(14, 2)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_projects_po_number ON projects (lower(po_number))",
    ]
    _apply_schema_changes(statements)


# Each process of the backend (4 api workers, the assistant, the start-up check) runs
# ensure_schema() when it starts. Changing a table locks it, so several processes doing it at
# once, while screens are reading, caused a deadlock (seen as Server error E-01C64378 on
# GET /projects during a fault test). Now each start first checks what is already there and
# only runs what's missing; that is done by one process at a time, and it gives up waiting
# after LOCK_TIMEOUT instead of blocking requests.
_SCHEMA_LOCK_ID = 7_302_118   # any fixed number: PostgreSQL advisory lock for schema changes
_LOCK_TIMEOUT = "5s"
_ADD_COLUMN = re.compile(r"ALTER TABLE (\w+) ADD COLUMN IF NOT EXISTS (\w+)", re.I)
_CREATE_INDEX = re.compile(r"CREATE (?:UNIQUE )?INDEX IF NOT EXISTS (\w+)", re.I)
_SET_DEFAULT = re.compile(r"ALTER TABLE (\w+) ALTER COLUMN (\w+) SET DEFAULT (.+)$", re.I)


def _missing(statements, columns, defaults, indexes):
    todo = []
    for sql in statements:
        m = _ADD_COLUMN.search(sql)
        if m and (m.group(1).lower(), m.group(2).lower()) in columns:
            continue
        m = _CREATE_INDEX.search(sql)
        if m and m.group(1).lower() in indexes:
            continue
        m = _SET_DEFAULT.search(sql)
        if m and (defaults.get((m.group(1).lower(), m.group(2).lower())) or "").lower() == m.group(3).strip().lower():
            continue
        todo.append(sql)
    return todo


def _current_schema(conn):
    rows = conn.execute(text(
        "SELECT table_name, column_name, column_default FROM information_schema.columns "
        "WHERE table_schema = current_schema()")).all()
    columns = {(t.lower(), c.lower()) for t, c, _ in rows}
    defaults = {(t.lower(), c.lower()): d for t, c, d in rows}
    indexes = {r[0].lower() for r in conn.execute(text(
        "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()")).all()}
    return columns, defaults, indexes


def _apply_schema_changes(statements):
    # Quick look without any lock: usually everything is already there, so nothing is changed
    with engine.connect() as conn:
        todo = _missing(statements, *_current_schema(conn))
    if not todo:
        return
    with engine.begin() as conn:
        conn.execute(text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"))
        conn.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": _SCHEMA_LOCK_ID})
        # Another process may have done it while this one waited for the lock: check again
        todo = _missing(statements, *_current_schema(conn))
        for sql in todo:
            conn.execute(text(sql))
        if todo:
            print(f"[schema] applied {len(todo)} database change(s)", flush=True)


def init_db():
    """Call this during startup to auto-create all tables and seed default superadmin."""
    Base.metadata.create_all(bind=engine)
    ensure_schema()

    import models
    from security import get_password_hash 

    db = SessionLocal()
    try:
        # Check if a superadmin account already exists
        superadmin = db.query(models.User).filter(models.User.is_superuser == True).first()
        
        if not superadmin:
            existing = db.query(models.User).filter(models.User.username == "admin").first()
            if existing:
                existing.is_superuser = True
                existing.is_active = True
                db.add(models.ActivityLog(
                    actor="system", action="promote_admin", entity_type="user",
                    entity_id=existing.id, summary="Promoted existing 'admin' account to superadmin",
                ))
                db.commit()
                print("--> Existing 'admin' account promoted to superadmin")
            else:
                admin_user = models.User(
                    username="admin",
                    name="System Superadmin",
                    hashed_password=get_password_hash(INITIAL_ADMIN_PASSWORD),
                    is_active=True,
                    is_superuser=True,
                    must_change_password=True,  # the first sign-in has to pick a new password
                )
                db.add(admin_user)
                db.flush()
                db.add(models.ActivityLog(
                    actor="system", action="promote_admin", entity_type="user",
                    entity_id=admin_user.id, summary="Created default superadmin 'admin'",
                ))
                db.commit()
                print("--> Default superadmin 'admin' created; it must change its password at first sign-in")
    except Exception as e:
        db.rollback()
        print(f"Error seeding default superadmin: {e}")
    finally:
        db.close()


# Password for the first admin account. Set ADMIN_INITIAL_PASSWORD in Backend/.env to use
# your own; either way the account has to change it at its first sign-in.
INITIAL_ADMIN_PASSWORD = os.getenv("ADMIN_INITIAL_PASSWORD", "").strip() or "admin123"
_KNOWN_PASSWORDS = ("admin123", "admin", "password", "12345678", "caic123")


def flag_default_passwords() -> None:
    """At startup: any admin account (or account named 'admin') still using a well-known
    password, such as the original admin123, must change it at its next sign-in. Only these
    few accounts are checked, because checking a password is deliberately slow."""
    import models
    from security import verify_password

    db = SessionLocal()
    try:
        users = (
            db.query(models.User)
            .filter(
                models.User.is_active.is_(True),
                models.User.must_change_password.is_(False),
                (models.User.is_superuser.is_(True)) | (models.User.username == "admin"),
            )
            .all()
        )
        for user in users:
            if any(verify_password(p, user.hashed_password) for p in _KNOWN_PASSWORDS):
                user.must_change_password = True
                print(f"--> '{user.username}' uses a well-known password; it must be changed at next sign-in")
        db.commit()
    except Exception as e:  # never stop the server over this check
        db.rollback()
        print(f"[startup] default-password check skipped: {e!r}")
    finally:
        db.close()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()