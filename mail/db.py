"""Connections and migrations. All mail tables live in their own schema (default `mail`)."""

from __future__ import annotations

import json
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
MIGRATION_LOCK = 6_161_001   # distinct from tracker (8421x) and kb (5151001)
NOTIFY_CHANNEL = "mail_events"


def connect(url: str, schema: str, autocommit: bool = False) -> psycopg.Connection:
    conn = psycopg.connect(url, autocommit=autocommit, row_factory=dict_row)
    conn.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema)))
    if not autocommit:
        conn.commit()
    return conn


def migrate(conn: psycopg.Connection, schema: str) -> list[str]:
    applied = []
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK,))
        conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
        conn.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(schema)))
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
                     "(name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
        done = {r["name"] for r in conn.execute("SELECT name FROM schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in done:
                continue
            conn.execute(path.read_text())
            conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
            applied.append(path.name)
    return applied


def audit(conn, actor: str, action: str, object_kind: str, object_id: str | None, detail: dict | None = None) -> None:
    conn.execute("INSERT INTO audit_log (actor, action, object_kind, object_id, detail) VALUES (%s,%s,%s,%s,%s)",
                 (actor, action, object_kind, object_id, Jsonb(detail or {})))


def emit(conn, kind: str, payload: dict) -> int:
    row = conn.execute("INSERT INTO events (kind, payload) VALUES (%s, %s) RETURNING id",
                       (kind, Jsonb(json.loads(json.dumps(payload, default=str))))).fetchone()
    conn.execute("SELECT pg_notify(%s, %s)", (NOTIFY_CHANNEL, str(row["id"])))
    return row["id"]


def get_setting(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = %s", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key: str, value) -> None:
    conn.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = "
                 "EXCLUDED.value", (key, Jsonb(value)))
