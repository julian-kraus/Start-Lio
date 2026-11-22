"""Connections and migrations for the planning store. All tables live in their own schema (default `plan`)."""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
MIGRATION_LOCK = 7_171_001   # distinct from tracker (84212), kb (5151001) and mail (6161001)
DEFAULT_SCHEMA = "plan"


def schema_name() -> str:
    return os.environ.get("PLAN_SCHEMA", DEFAULT_SCHEMA)


def connect(url: str, schema: str | None = None, autocommit: bool = False) -> psycopg.Connection:
    conn = psycopg.connect(url, autocommit=autocommit, row_factory=dict_row)
    conn.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema or schema_name())))
    if not autocommit:
        conn.commit()
    return conn


def migrate(conn: psycopg.Connection, schema: str | None = None) -> list[str]:
    """Apply pending migrations in name order under the plan advisory lock. Safe to run repeatedly."""
    schema = schema or schema_name()
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
