"""plan schema against a local throwaway Postgres, in a temporary schema dropped afterwards. Synthetic data only."""

import os
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from psycopg import sql

from plan import clock, db

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")

TABLES = {"goals", "clock", "runs", "weak_spots", "offers", "promises", "checkins", "reliability",
          "recommendations", "decisions", "timeline_events", "agent_traces"}
T0 = datetime(2030, 1, 10, 8, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.delenv(clock.ENV_VAR, raising=False)
    schema = f"plan_test_{uuid.uuid4().hex[:8]}"
    c = db.connect(URL, schema)
    try:
        yield c, schema
    finally:
        c.rollback()
        c.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
        c.commit()
        c.close()


def test_migrate_creates_tables_and_is_idempotent(conn):
    c, schema = conn
    assert db.migrate(c, schema) == ["001_core.sql"]
    assert db.migrate(c, schema) == []
    rows = c.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = %s", (schema,))
    assert TABLES <= {r["table_name"] for r in rows}


def test_migrate_holds_its_own_advisory_lock(conn):
    c, schema = conn
    assert db.MIGRATION_LOCK not in (84212, 5151001, 6161001)
    other = psycopg.connect(URL, autocommit=True)
    try:
        other.execute("SELECT pg_advisory_lock(%s)", (db.MIGRATION_LOCK,))
        c.execute("SET lock_timeout = '300ms'")
        c.commit()
        with pytest.raises(psycopg.errors.LockNotAvailable):
            db.migrate(c, schema)
        c.rollback()
        other.execute("SELECT pg_advisory_unlock(%s)", (db.MIGRATION_LOCK,))
        assert db.migrate(c, schema) == ["001_core.sql"]
    finally:
        other.close()


def test_scenario_clock_roundtrip(conn):
    c, schema = conn
    db.migrate(c, schema)
    assert clock.read(c)["mode"] == "real" and not clock.is_scenario(c)
    set_at = T0 + timedelta(days=3)
    clock.set_scenario(c, "2030-01-10T08:00:00+00:00", rate=60, by="test", real=lambda: set_at)
    c.commit()
    assert clock.is_scenario(c)
    assert clock.now(c, real=lambda: set_at + timedelta(minutes=2)) == T0 + timedelta(hours=2)
    clock.set_real(c, by="test")
    c.commit()
    assert not clock.is_scenario(c)
    assert abs(clock.now(c) - datetime.now(timezone.utc)) < timedelta(seconds=5)


def test_constraints_guard_the_contract(conn):
    c, schema = conn
    db.migrate(c, schema)
    run = c.execute("INSERT INTO runs (kind, clock_now) VALUES ('rank', %s) RETURNING run_id", (T0,)).fetchone()
    rec = c.execute(
        "INSERT INTO recommendations (run_id, kind, part_id, p_fail, loss_per_copy_eur, commitment_eur, cost_model, "
        "decide_by, rationale, sources) VALUES (%s, 'hedge', 'PART-A', 0.3, 60, 600, '{\"kind\": \"per_unit\"}', %s, "
        "'r', '[{\"fact_id\": \"f1\"}]') RETURNING recommendation_id", (run["run_id"], T0)).fetchone()
    c.execute("INSERT INTO decisions (decision_id, recommendation_id, title) VALUES ('D-1', %s, 't')",
              (rec["recommendation_id"],))
    c.execute("INSERT INTO promises (promise_id, supplier_id, what, due, decision_id) VALUES ('P-1', 'S', 'w', %s, 'D-1')",
              (T0,))
    c.execute("INSERT INTO checkins (promise_id, at, outcome) VALUES ('P-1', %s, 'on_track')", (T0,))
    c.commit()
    with pytest.raises(psycopg.errors.CheckViolation):
        c.execute("UPDATE decisions SET status = 'accepted' WHERE decision_id = 'D-1'")
    c.rollback()
    with pytest.raises(psycopg.errors.CheckViolation):
        c.execute("INSERT INTO clock (id) VALUES (2)")
    c.rollback()
