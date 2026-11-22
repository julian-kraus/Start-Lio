"""The lab database: a local-only Postgres that holds the kb, mail, plan and lab schemas, seeded from a portal
snapshot instead of the live tracker.

Never the live database. `assert_local` refuses anything that is not localhost or a Docker service name, and
anything that looks like Supabase or a pooled connection, the same guard the tests and `ops/check.py` use."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from tracker.config import PROJECT_ROOT, load_env

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
MIGRATION_LOCK = 9_191_001   # distinct from tracker (84212), kb (5151001), mail (6161001), plan (7171001)
SCHEMA_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
SNAPSHOT_DIR = PROJECT_ROOT / "data" / "lab"
_FORBIDDEN = ("supabase", "pooler")
_LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "::1"}

# The managed lab-db docker service, used only when no explicit database URL is given. The defaults match the
# `lab-db` service in docker-compose.yml; every knob is an env var so the port and name can move without edits.
LAB_DB_CONTAINER = os.environ.get("LAB_DB_CONTAINER", "lookahead-lab-db")
LAB_DB_PORT = os.environ.get("LAB_DB_PORT", "5441")
LAB_DB_USER = os.environ.get("LAB_DB_USER", "lab")
LAB_DB_PASSWORD = os.environ.get("LAB_DB_PASSWORD", "lab")
LAB_DB_NAME = os.environ.get("LAB_DB_NAME", "lab")
_READY_TIMEOUT = float(os.environ.get("LAB_DB_READY_TIMEOUT", "60"))


@dataclass(frozen=True)
class Schemas:
    kb: str
    mail: str
    plan: str
    lab: str

    def as_list(self) -> list[str]:
        return [self.kb, self.mail, self.plan, self.lab]


def schemas() -> Schemas:
    """The four schema names, each overridable by its package's env var so tests can isolate a run."""
    s = Schemas(os.environ.get("KB_SCHEMA", "kb"), os.environ.get("MAIL_SCHEMA", "mail"),
                os.environ.get("PLAN_SCHEMA", "plan"), os.environ.get("LAB_SCHEMA", "lab"))
    for name in s.as_list():
        if not SCHEMA_NAME.match(name):
            raise SystemExit(f"invalid schema name {name!r}")
    return s


def docker_url() -> str:
    """The URL of the managed lab-db docker service."""
    user = urllib.parse.quote(LAB_DB_USER)
    password = urllib.parse.quote(LAB_DB_PASSWORD, safe="")
    return f"postgresql://{user}:{password}@127.0.0.1:{LAB_DB_PORT}/{LAB_DB_NAME}"


def resolve_url() -> tuple[str, bool]:
    """(url, managed). managed is True for the lab-db docker service (which `init` may start); it is False for an
    already-running local database named explicitly by LAB_DATABASE_URL, TEST_DATABASE_URL or DATABASE_URL."""
    load_env(PROJECT_ROOT / ".env")
    for var in ("LAB_DATABASE_URL", "TEST_DATABASE_URL", "DATABASE_URL"):
        if os.environ.get(var):
            return os.environ[var], False
    return docker_url(), True


def assert_local(url: str) -> None:
    """Refuse a URL that is not a local throwaway Postgres. The lab never touches shared infrastructure."""
    low = url.lower()
    for bad in _FORBIDDEN:
        if bad in low:
            raise SystemExit(f"refusing a managed or pooled database URL (contains {bad!r}); the lab is local only")
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if host in _LOCAL_HOSTS:
        return
    if "." in host or ":" in host:   # a domain name or a non-loopback IP (v4 dotted or v6 with colons)
        raise SystemExit(f"refusing non-local database host {host!r}; use localhost or a Docker service name")
    return   # a bare name with no dot is a Docker service name on the compose network


def connect(url: str, schema: str, autocommit: bool = False) -> psycopg.Connection:
    """Connection whose search_path resolves the given schema first and the tracker's public tables second."""
    assert_local(url)
    conn = psycopg.connect(url, autocommit=autocommit, row_factory=dict_row)
    conn.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema)))
    if not autocommit:
        conn.commit()
    return conn


def migrate(conn: psycopg.Connection, schema: str) -> list[str]:
    """Apply pending SQL files from lab/migrations in name order. Returns the names applied."""
    applied: list[str] = []
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK,))
        conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
        conn.execute(sql.SQL(
            "CREATE TABLE IF NOT EXISTS {}.schema_migrations "
            "(name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        ).format(sql.Identifier(schema)))
        done = {r["name"] for r in conn.execute(
            sql.SQL("SELECT name FROM {}.schema_migrations").format(sql.Identifier(schema)))}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in done:
                continue
            conn.execute(path.read_text())
            conn.execute(sql.SQL("INSERT INTO {}.schema_migrations (name) VALUES (%s)").format(
                sql.Identifier(schema)), (path.name,))
            applied.append(path.name)
    return applied


def create_schemas(url: str, s: Schemas) -> dict[str, list[str]]:
    """Create or update the kb, mail, plan and lab schemas with their own migrations. Idempotent."""
    assert_local(url)
    from kb import db as kb_db
    from mail import db as mail_db
    from plan import db as plan_db
    applied: dict[str, list[str]] = {}
    for name, mod, schema in (("kb", kb_db, s.kb), ("mail", mail_db, s.mail), ("plan", plan_db, s.plan)):
        c = mod.connect(url, schema)
        try:
            applied[name] = mod.migrate(c, schema)
        finally:
            c.close()
    c = connect(url, s.lab)
    try:
        applied["lab"] = migrate(c, s.lab)
    finally:
        c.close()
    return applied


def ensure_managed_db() -> str:
    """Start or reuse the lab-db docker service and return its URL. Used when no explicit URL was given."""
    if shutil.which("docker") is None:
        raise SystemExit("docker is required to start the lab-db service; "
                         "set LAB_DATABASE_URL or TEST_DATABASE_URL to use an existing local Postgres")
    up = subprocess.run(["docker", "compose", "up", "-d", "lab-db"],
                        cwd=PROJECT_ROOT, capture_output=True, text=True)
    if up.returncode != 0:
        raise SystemExit(f"could not start the lab-db service: {up.stderr.strip()[:200]}")
    url = docker_url()
    deadline = time.monotonic() + _READY_TIMEOUT
    while True:
        try:
            psycopg.connect(url).close()
            return url
        except psycopg.OperationalError:
            if time.monotonic() >= deadline:
                raise SystemExit(f"lab-db did not become ready within {int(_READY_TIMEOUT)}s")
            time.sleep(1)


def _container_running(name: str) -> bool:
    p = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", name], capture_output=True, text=True)
    return p.returncode == 0 and p.stdout.strip() == "true"


def _schema_args(s: Schemas) -> list[str]:
    args: list[str] = []
    for name in s.as_list():
        args += ["-n", name]
    return args


def snapshot(url: str, name: str, s: Schemas, managed: bool, out_dir: Path = SNAPSHOT_DIR) -> Path:
    """pg_dump the four lab schemas to a custom-format file under data/lab. Prefers a host pg_dump; falls back to
    the managed lab-db container over a pipe when the host has no client tools."""
    assert_local(url)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{name}.dump"
    dump_args = ["-Fc", "--no-owner", "--no-privileges", *_schema_args(s)]
    if shutil.which("pg_dump"):
        _run(["pg_dump", "-d", url, *dump_args, "-f", str(target)])
    elif managed and _container_running(LAB_DB_CONTAINER):
        # inside the container, the local socket is trust-authenticated, so no password is needed or exposed
        data = _run(["docker", "exec", LAB_DB_CONTAINER, "pg_dump",
                     "-U", LAB_DB_USER, "-d", LAB_DB_NAME, *dump_args], capture=True)
        target.write_bytes(data)
    else:
        raise SystemExit("pg_dump not found; install the libpq client tools or use the managed lab-db service")
    return target


def restore(url: str, name: str, s: Schemas, managed: bool, out_dir: Path = SNAPSHOT_DIR) -> Path:
    """Drop the four lab schemas and restore them from a snapshot, so a scenario starts from the same state."""
    assert_local(url)
    target = out_dir / f"{name}.dump"
    if not target.is_file():
        raise SystemExit(f"no snapshot {target}")
    c = connect(url, s.lab, autocommit=True)
    try:
        for schema in s.as_list():
            c.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
    finally:
        c.close()
    if shutil.which("pg_restore"):
        _run(["pg_restore", "--no-owner", "--no-privileges", "-d", url, str(target)])
    elif managed and _container_running(LAB_DB_CONTAINER):
        _run(["docker", "exec", "-i", LAB_DB_CONTAINER, "pg_restore", "--no-owner", "--no-privileges",
              "-U", LAB_DB_USER, "-d", LAB_DB_NAME], stdin=target.read_bytes())
    else:
        raise SystemExit("pg_restore not found; install the libpq client tools or use the managed lab-db service")
    return target


def _run(cmd: list[str], capture: bool = False, stdin: bytes | None = None) -> bytes:
    proc = subprocess.run(cmd, input=stdin, capture_output=True)
    if proc.returncode != 0:
        raise SystemExit(f"{cmd[0]} failed: {proc.stderr.decode(errors='replace').strip()[:300]}")
    return proc.stdout
