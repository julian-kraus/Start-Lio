"""Postgres schema and queries. Postgres is both the history and the event log (outbox)."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

NOTIFY_CHANNEL = "file_events"
SCHEMA_LOCK = 84211
POLLER_LOCK = 84212   # held for the lifetime of a poller process: only one poller at a time
EVENTS_LOCK = 84213   # held while writing events, so event ids always commit in order

SCHEMA = """
CREATE TABLE IF NOT EXISTS polls (
    id          bigserial PRIMARY KEY,
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    ok          boolean,
    baseline    boolean NOT NULL DEFAULT false,
    n_files     integer,
    n_events    integer,
    error       text,
    failed      jsonb,
    note        text
);
ALTER TABLE polls ADD COLUMN IF NOT EXISTS failed jsonb;
ALTER TABLE polls ADD COLUMN IF NOT EXISTS note text;

CREATE TABLE IF NOT EXISTS file_versions (
    id            bigserial PRIMARY KEY,
    path          text NOT NULL,
    version       integer NOT NULL,
    sha256        text NOT NULL,
    bytes         bigint NOT NULL,
    first_seen    timestamptz NOT NULL,
    last_seen     timestamptz NOT NULL,
    superseded_at timestamptz,
    UNIQUE (path, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS file_versions_current ON file_versions (path) WHERE superseded_at IS NULL;

CREATE TABLE IF NOT EXISTS events (
    id       bigserial PRIMARY KEY,
    at       timestamptz NOT NULL DEFAULT now(),
    poll_id  bigint REFERENCES polls(id),
    kind     text NOT NULL CHECK (kind IN ('added','changed','removed','renamed','stale','recovered')),
    path     text NOT NULL,
    old_path text,
    old_sha  text,
    new_sha  text,
    summary  text NOT NULL,
    diff     jsonb
);
CREATE INDEX IF NOT EXISTS events_path ON events (path);
CREATE INDEX IF NOT EXISTS events_at ON events (at);

CREATE TABLE IF NOT EXISTS stale_flags (
    path       text PRIMARY KEY,
    flagged_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS consumer_cursors (
    consumer      text PRIMARY KEY,
    last_event_id bigint NOT NULL,
    updated_at    timestamptz NOT NULL DEFAULT now()
);
"""


@dataclass(frozen=True)
class CurrentFile:
    path: str
    sha256: str
    bytes: int
    version: int
    first_seen: datetime


def connect(url: str, autocommit: bool = False) -> psycopg.Connection:
    return psycopg.connect(url, autocommit=autocommit, row_factory=dict_row)


def init_schema(conn: psycopg.Connection) -> None:
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK,))
        conn.execute(SCHEMA)


def current_state(conn: psycopg.Connection) -> dict[str, CurrentFile]:
    rows = conn.execute(
        "SELECT path, sha256, bytes, version, first_seen FROM file_versions WHERE superseded_at IS NULL"
    ).fetchall()
    return {r["path"]: CurrentFile(**r) for r in rows}


def has_baseline(conn: psycopg.Connection) -> bool:
    return conn.execute("SELECT EXISTS (SELECT 1 FROM polls WHERE ok)").fetchone()["exists"]


def start_poll(conn: psycopg.Connection, baseline: bool = False) -> int:
    row = conn.execute("INSERT INTO polls (baseline) VALUES (%s) RETURNING id", (baseline,)).fetchone()
    conn.commit()
    return row["id"]


def finish_poll(conn, poll_id: int, ok: bool, n_files: int | None, n_events: int | None, error: str | None,
                failed: list[dict] | None = None, note: str | None = None) -> None:
    conn.execute(
        """UPDATE polls SET finished_at = now(), ok = %s, n_files = %s, n_events = %s, error = %s,
           failed = %s, note = coalesce(%s, note) WHERE id = %s""",
        (ok, n_files, n_events, error, Jsonb(failed) if failed else None, note, poll_id),
    )


def try_poller_lock(conn) -> bool:
    """Session-level lock: released automatically when the poller's connection closes."""
    return conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (POLLER_LOCK,)).fetchone()["ok"]


def lock_events(conn) -> None:
    """Serialise event writers inside the current transaction. Ids are drawn after the lock is taken,
    so they commit in increasing order and cursor-based consumers ('id > cursor') never skip one."""
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (EVENTS_LOCK,))


def add_version(conn, path: str, sha: str, size: int, now: datetime) -> None:
    """Supersede the current version of path (if any) and insert the new one."""
    conn.execute(
        "UPDATE file_versions SET superseded_at = %s WHERE path = %s AND superseded_at IS NULL", (now, path)
    )
    conn.execute(
        """INSERT INTO file_versions (path, version, sha256, bytes, first_seen, last_seen)
           SELECT %s, coalesce(max(version), 0) + 1, %s, %s, %s, %s FROM file_versions WHERE path = %s""",
        (path, sha, size, now, now, path),
    )


def remove_version(conn, path: str, now: datetime) -> None:
    conn.execute(
        "UPDATE file_versions SET superseded_at = %s WHERE path = %s AND superseded_at IS NULL", (now, path)
    )


def _jsonb_safe(value):
    """jsonb rejects NaN/Infinity and NUL characters, which PDFs and spreadsheets can contain."""
    if isinstance(value, dict):
        return {str(k): _jsonb_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonb_safe(v) for v in value]
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return str(value)
    if isinstance(value, str):
        return value.replace("\x00", "")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def insert_event(conn, poll_id: int, kind: str, path: str, summary: str, *, old_path=None,
                 old_sha=None, new_sha=None, diff=None, at: datetime | None = None) -> int:
    row = conn.execute(
        """INSERT INTO events (at, poll_id, kind, path, old_path, old_sha, new_sha, summary, diff)
           VALUES (coalesce(%s, now()), %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (at, poll_id, kind, path, old_path, old_sha, new_sha, summary.replace("\x00", ""),
         Jsonb(_jsonb_safe(diff)) if diff is not None else None),
    ).fetchone()
    payload = json.dumps({"id": row["id"], "kind": kind, "path": path})
    conn.execute("SELECT pg_notify(%s, %s)", (NOTIFY_CHANNEL, payload))
    return row["id"]


def stale_flags(conn) -> dict[str, datetime]:
    return {r["path"]: r["flagged_at"] for r in conn.execute("SELECT path, flagged_at FROM stale_flags")}


def set_stale_flag(conn, path: str, now: datetime) -> None:
    conn.execute("INSERT INTO stale_flags (path, flagged_at) VALUES (%s, %s) ON CONFLICT DO NOTHING", (path, now))


def clear_stale_flag(conn, path: str) -> None:
    conn.execute("DELETE FROM stale_flags WHERE path = %s", (path,))


def last_successful_poll(conn) -> datetime | None:
    row = conn.execute("SELECT max(finished_at) AS t FROM polls WHERE ok").fetchone()
    return row["t"]


def fetch_events(conn, after_id: int, limit: int = 500) -> list[dict]:
    return conn.execute(
        "SELECT * FROM events WHERE id > %s ORDER BY id LIMIT %s", (after_id, limit)
    ).fetchall()


def get_cursor(conn, consumer: str, start: str = "latest") -> int:
    """Return the consumer's cursor, creating it at the latest event (or 0 for 'beginning')."""
    initial = 0
    if start == "latest":
        initial = conn.execute("SELECT coalesce(max(id), 0) AS m FROM events").fetchone()["m"]
    conn.execute(
        "INSERT INTO consumer_cursors (consumer, last_event_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (consumer, initial),
    )
    return conn.execute(
        "SELECT last_event_id FROM consumer_cursors WHERE consumer = %s", (consumer,)
    ).fetchone()["last_event_id"]


def set_cursor(conn, consumer: str, event_id: int) -> None:
    conn.execute(
        "UPDATE consumer_cursors SET last_event_id = %s, updated_at = now() WHERE consumer = %s",
        (event_id, consumer),
    )
