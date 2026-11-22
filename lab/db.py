"""The lab database: a local-only Postgres that holds the kb, mail, plan and lab schemas, seeded from a portal
snapshot instead of the live tracker.

Never the live database. `assert_local` refuses anything that libpq would not connect to on the local host, and
anything that looks like Supabase or a pooled connection, the same intent the tests and `ops/check.py` have. It
reads the host the way libpq does (every `host` and `hostaddr`, the `PGHOST`, `PGHOSTADDR` and `PGSERVICE`
environment, and numeric address forms) so a crafted URL cannot slip a remote host past it."""

from __future__ import annotations

import ipaddress
import os
import re
import shutil
import subprocess
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg import conninfo, sql
from psycopg.rows import dict_row

from tracker.config import PROJECT_ROOT, load_env

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
MIGRATION_LOCK = 9_191_001   # distinct from tracker (84212), kb (5151001), mail (6161001), plan (7171001)
SCHEMA_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
SNAPSHOT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SNAPSHOT_DIR = PROJECT_ROOT / "data" / "lab"
DUMP_MAGIC = b"PGDMP"
_FORBIDDEN = ("supabase", "pooler")
_LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "::1"}

# The managed lab-db docker compose service, used only when no explicit database URL is given. The defaults match
# the `lab-db` service in docker-compose.yml; every knob is an env var so the port and name can move without edits.
# The service is addressed through `docker compose`, so no container name is assumed.
LAB_DB_SERVICE = os.environ.get("LAB_DB_SERVICE", "lab-db")
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
    """(url, managed). managed is True for the lab-db docker service (which `init` may start). Only the dedicated
    `LAB_DATABASE_URL` names an existing database: `TEST_DATABASE_URL` or `DATABASE_URL` are never used, so a shared
    test or tracker database can never be seeded over or dropped by a restore."""
    load_env(PROJECT_ROOT / ".env")
    url = os.environ.get("LAB_DATABASE_URL")
    if url:
        return url, False
    return docker_url(), True


def db_label(url: str) -> str:
    """A label for an explicit URL that never carries the password: host, port and database only."""
    p = conninfo.conninfo_to_dict(url)
    return f"{p.get('host') or 'localhost'}:{p.get('port') or '5432'}/{p.get('dbname') or ''}"


def _split(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def _assert_local_host(host: str) -> None:
    h = host.strip().lower()
    if h in _LOCAL_HOSTS:
        return
    try:
        if ipaddress.ip_address(h).is_loopback:
            return
        raise SystemExit(f"refusing non-local database host {host!r}; use localhost or a Docker service name")
    except ValueError:
        pass
    if re.fullmatch(r"0[xX][0-9a-fA-F]+|\d+", h):   # a decimal or hex integer that libpq would read as an address
        raise SystemExit(f"refusing numeric database host {host!r}; use localhost or a Docker service name")
    if "." in h or ":" in h:   # a domain name or a non-loopback IP (v4 dotted or v6 with colons)
        raise SystemExit(f"refusing non-local database host {host!r}; use localhost or a Docker service name")
    return   # a bare name with no dot is a Docker service name on the compose network


def assert_local(url: str) -> None:
    """Refuse a URL that is not a local throwaway Postgres. The lab never touches shared infrastructure."""
    low = url.lower()
    for bad in _FORBIDDEN:
        if bad in low:
            raise SystemExit(f"refusing a managed or pooled database URL (contains {bad!r}); the lab is local only")
    try:
        params = conninfo.conninfo_to_dict(url)
    except psycopg.Error as exc:
        raise SystemExit(f"refusing an unparseable database URL: {exc}")
    hosts = _split(params.get("host") or os.environ.get("PGHOST"))
    hostaddrs = _split(params.get("hostaddr") or os.environ.get("PGHOSTADDR"))
    if not hosts and not hostaddrs:
        # No host anywhere means a local unix socket, unless a service file (which we cannot read) could set one.
        if params.get("service") or os.environ.get("PGSERVICE"):
            raise SystemExit("refusing a service-based connection; name an explicit local host for the lab")
        return
    for host in hostaddrs + hosts:
        _assert_local_host(host)


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
                         "set LAB_DATABASE_URL to use an existing local Postgres")
    up = subprocess.run(["docker", "compose", "up", "-d", LAB_DB_SERVICE],
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


def _compose_service_running() -> bool:
    p = subprocess.run(["docker", "compose", "ps", "--status", "running", "-q", LAB_DB_SERVICE],
                       cwd=PROJECT_ROOT, capture_output=True, text=True)
    return p.returncode == 0 and bool(p.stdout.strip())


def _schema_args(s: Schemas) -> list[str]:
    args: list[str] = []
    for name in s.as_list():
        args += ["-n", name]
    return args


def _safe_snapshot_name(name: str) -> str:
    if ".." in name or not SNAPSHOT_NAME.match(name):
        raise SystemExit(f"invalid snapshot name {name!r}; use letters, digits, '.', '_' or '-' (no path parts)")
    return name


def _dump_place(managed: bool, verb: str) -> str:
    """Decide where a dump tool runs: on the host if the client tools are installed, else inside the managed
    container. Resolved before any work so a restore never drops a schema it then cannot refill."""
    if shutil.which(verb):
        return "host"
    if managed and _compose_service_running():
        return "compose"
    raise SystemExit(f"{verb} not found; install the libpq client tools or use the managed lab-db service")


def snapshot(url: str, name: str, s: Schemas, managed: bool, out_dir: Path = SNAPSHOT_DIR) -> Path:
    """pg_dump the four lab schemas to a custom-format file under data/lab. Prefers a host pg_dump; falls back to
    the managed lab-db container over a pipe when the host has no client tools."""
    assert_local(url)
    name = _safe_snapshot_name(name)
    place = _dump_place(managed, "pg_dump")
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{name}.dump"
    dump_args = ["-Fc", "--no-owner", "--no-privileges", *_schema_args(s)]
    if place == "host":
        _run(["pg_dump", "-d", url, *dump_args, "-f", str(target)])
    else:
        # inside the container the local socket is trust-authenticated, so no password is needed or exposed
        data = _run(["docker", "compose", "exec", "-T", LAB_DB_SERVICE, "pg_dump",
                     "-U", LAB_DB_USER, "-d", LAB_DB_NAME, *dump_args], capture=True, cwd=PROJECT_ROOT)
        target.write_bytes(data)
    return target


def _validate_dump_header(target: Path) -> None:
    with open(target, "rb") as fh:
        if fh.read(len(DUMP_MAGIC)) != DUMP_MAGIC:
            raise SystemExit(f"{target} is not a custom-format pg_dump archive; refusing to restore")


def restore(url: str, name: str, s: Schemas, managed: bool, out_dir: Path = SNAPSHOT_DIR) -> Path:
    """Restore the four lab schemas from a snapshot so a scenario starts from the same state.

    The restore is atomic: the tool is resolved and the archive is checked before anything is dropped, and the old
    objects are dropped and reloaded inside a single transaction (`--clean --if-exists --single-transaction`). A
    failure rolls the whole thing back, so a bad dump leaves the existing data untouched."""
    assert_local(url)
    name = _safe_snapshot_name(name)
    target = out_dir / f"{name}.dump"
    if not target.is_file():
        raise SystemExit(f"no snapshot {target}")
    _validate_dump_header(target)
    place = _dump_place(managed, "pg_restore")
    restore_args = ["--clean", "--if-exists", "--single-transaction", "--no-owner", "--no-privileges",
                    *_schema_args(s)]
    if place == "host":
        _run(["pg_restore", "-l", str(target)])   # reject a corrupt archive before touching the database
        _run(["pg_restore", "-d", url, *restore_args, str(target)])
    else:
        data = target.read_bytes()
        _run(["docker", "compose", "exec", "-T", LAB_DB_SERVICE, "pg_restore", "-l"],
             stdin=data, cwd=PROJECT_ROOT)
        _run(["docker", "compose", "exec", "-T", LAB_DB_SERVICE, "pg_restore",
              "-U", LAB_DB_USER, "-d", LAB_DB_NAME, *restore_args], stdin=data, cwd=PROJECT_ROOT)
    return target


def _run(cmd: list[str], capture: bool = False, stdin: bytes | None = None, cwd: Path | None = None) -> bytes:
    proc = subprocess.run(cmd, input=stdin, capture_output=True, cwd=cwd)
    if proc.returncode != 0:
        raise SystemExit(f"{cmd[0]} failed: {proc.stderr.decode(errors='replace').strip()[:300]}")
    return proc.stdout
