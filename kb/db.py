"""Connections and migrations. All kb tables live in their own schema (default `kb`) next to the tracker's tables."""

from __future__ import annotations

from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
MIGRATION_LOCK = 5_151_001   # distinct from the tracker's 8421x locks
ISSUE_CHANNEL = "kb_issues"


def connect(url: str, schema: str, autocommit: bool = False) -> psycopg.Connection:
    """Connection whose search_path resolves kb tables first and tracker tables (public) second."""
    conn = psycopg.connect(url, autocommit=autocommit, row_factory=dict_row)
    conn.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema)))
    if not autocommit:
        conn.commit()
    return conn


def migrate(conn: psycopg.Connection, schema: str) -> list[str]:
    """Apply pending SQL files from kb/migrations in name order. Returns the names applied."""
    applied = []
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


def tracker_table_exists(conn: psycopg.Connection, name: str) -> bool:
    row = conn.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (f"public.{name}",)).fetchone()
    return bool(row["ok"])
