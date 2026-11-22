"""The scenario lab (L0): schema creation, seeding from a snapshot through kb.ingest, status, and
snapshot/restore. Runs against the local throwaway Postgres in temporary schemas. Synthetic data only."""

import os
import shutil
import uuid

import psycopg
import pytest
from psycopg import sql

from conftest import make_pdf
from lab import db
from lab import seed as seed_mod

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")

LAB_TABLES = {"runs", "injections", "schema_migrations"}


@pytest.fixture
def lab(monkeypatch):
    sfx = uuid.uuid4().hex[:8]
    s = db.Schemas(f"kb_{sfx}", f"mail_{sfx}", f"plan_{sfx}", f"lab_{sfx}")
    for var, name in (("KB_SCHEMA", s.kb), ("MAIL_SCHEMA", s.mail), ("PLAN_SCHEMA", s.plan), ("LAB_SCHEMA", s.lab)):
        monkeypatch.setenv(var, name)
    monkeypatch.setenv("LAB_DATABASE_URL", URL)
    monkeypatch.delenv("LOOKAHEAD_SCENARIO_NOW", raising=False)
    try:
        yield s
    finally:
        c = db.connect(URL, "public", autocommit=True)
        for schema in s.as_list():
            c.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
        c.close()


def _regclass(conn, qualified: str) -> bool:
    return conn.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (qualified,)).fetchone()["ok"]


def test_init_creates_every_schema_and_is_idempotent(lab):
    applied = db.create_schemas(URL, lab)
    assert applied["lab"] == ["001_core.sql"]
    assert applied["kb"] and applied["mail"] and applied["plan"]   # each package applied its own migrations
    c = db.connect(URL, lab.lab)
    try:
        assert _regclass(c, f"{lab.kb}.facts")
        assert _regclass(c, f"{lab.mail}.messages")
        assert _regclass(c, f"{lab.plan}.clock")
        tables = {r["table_name"] for r in c.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s", (lab.lab,))}
        assert LAB_TABLES <= tables
    finally:
        c.close()
    assert db.create_schemas(URL, lab)["lab"] == []   # second run adds nothing


def test_assert_local_refuses_shared_and_remote_urls():
    for bad in (
        "postgresql://u:p@db.pooler.supabase.com:5432/postgres",
        "postgresql://u:p@aws-0-eu-central-1.pooler.supabase.com:6543/postgres",
        "postgresql://u:p@203.0.113.9:5432/live",
        "postgresql://u:p@db.example.com/live",
    ):
        with pytest.raises(SystemExit):
            db.assert_local(bad)
    for ok in (
        "postgresql://u:p@localhost:5432/x",
        "postgresql://u:p@127.0.0.1:5440/check",
        "postgresql://lab:lab@lab-db:5432/lab",
    ):
        db.assert_local(ok)   # a local host or a Docker service name is allowed


def test_seed_ingests_through_kb_and_is_idempotent(lab, tmp_path):
    current = tmp_path / "data" / "current" / "soony" / "docs"
    current.mkdir(parents=True)
    (current / "note.pdf").write_bytes(make_pdf(["Delivery note", "Item ABC 10 units"]))
    snapshot = tmp_path / "data" / "current"

    db.create_schemas(URL, lab)
    first = seed_mod.seed(URL, lab, seed_mod.data_root(snapshot))
    assert sum(first["sync"].values()) >= 1   # the file was processed through kb.ingest

    c = db.connect(URL, lab.kb)
    try:
        docs = c.execute("SELECT count(*) AS n FROM documents WHERE active").fetchone()["n"]
        assert docs >= 1
        second = seed_mod.seed(URL, lab, seed_mod.data_root(snapshot))
        assert second["sync"] == {}   # nothing re-ingested on a second seed
        assert c.execute("SELECT count(*) AS n FROM documents WHERE active").fetchone()["n"] == docs
    finally:
        c.close()


def test_data_root_accepts_both_forms(tmp_path):
    (tmp_path / "current").mkdir()
    assert seed_mod.data_root(tmp_path) == tmp_path.resolve()
    assert seed_mod.data_root(tmp_path / "current") == tmp_path.resolve()
    with pytest.raises(SystemExit):
        seed_mod.data_root(tmp_path / "nope")


def test_status_reports_the_expected_shape(lab):
    db.create_schemas(URL, lab)
    st = seed_mod.status(URL, lab)
    assert {"files", "facts", "believed", "open_issues", "mail", "lookahead_records",
            "runs", "injections", "scenario_clock"} <= set(st)
    assert st["runs"] == 0 and st["injections"] == 0
    assert set(st["mail"]) == {"in", "out"}
    assert st["scenario_clock"]["mode"] in ("real", "scenario")


def test_injections_are_append_only_under_a_run(lab):
    db.create_schemas(URL, lab)
    c = db.connect(URL, lab.lab, autocommit=True)
    try:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            c.execute("INSERT INTO injections (run, event_type) VALUES (%s, %s)", (424242, "offer"))
        run_id = c.execute("INSERT INTO runs (scenario) VALUES ('base') RETURNING id").fetchone()["id"]
        c.execute("INSERT INTO injections (run, event_type, parameters) VALUES (%s, %s, %s)",
                  (run_id, "offer", "{}"))
        assert c.execute("SELECT count(*) AS n FROM injections").fetchone()["n"] == 1
    finally:
        c.close()


@pytest.mark.skipif(not (shutil.which("pg_dump") and shutil.which("pg_restore")),
                    reason="libpq client tools (pg_dump/pg_restore) not installed")
def test_snapshot_and_restore_round_trip(lab, tmp_path):
    db.create_schemas(URL, lab)
    out = tmp_path / "snaps"
    c = db.connect(URL, lab.lab, autocommit=True)
    try:
        c.execute("INSERT INTO runs (scenario) VALUES ('base')")
        db.snapshot(URL, "s1", lab, managed=False, out_dir=out)
        c.execute("INSERT INTO runs (scenario) VALUES ('after-snapshot')")
        assert c.execute("SELECT count(*) AS n FROM runs").fetchone()["n"] == 2
    finally:
        c.close()
    db.restore(URL, "s1", lab, managed=False, out_dir=out)
    c = db.connect(URL, lab.lab)
    try:
        rows = c.execute("SELECT scenario FROM runs").fetchall()
        assert [r["scenario"] for r in rows] == ["base"]   # the post-snapshot write is gone
    finally:
        c.close()
