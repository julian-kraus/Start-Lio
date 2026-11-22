"""The persona's score is the reliability of a real supplier's history in kb. Synthetic facts only."""

import importlib.util
import os
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import conninfo, sql
from psycopg.types.json import Jsonb

from kb import db as kb_db
from plan import contracts, db as plan_db
from tracker.lookahead import Lookahead, append_records

reliability = pytest.importorskip("sourcing.reliability", reason="sourcing/reliability is not on this base")

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("demo_reset", HERE.parents[1] / "ops" / "demo" / "reset.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)

URL = os.environ.get("TEST_DATABASE_URL")
DEADLINE = "2031-03-09T20:00:00+01:00"
VALUES = {"unit_loss": "7", "budget": "300", "part": "printed booklet", "offer_quantity": "250", "offer_price": "2.5"}


@pytest.fixture
def demo_db():
    if not URL:
        pytest.skip("TEST_DATABASE_URL not set")
    if conninfo.conninfo_to_dict(URL).get("host") not in demo.LOOPBACK:
        pytest.skip("only a local throwaway Postgres")
    name = f"pack_{uuid.uuid4().hex[:8]}_demo"
    with psycopg.connect(URL, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            yield conninfo.make_conninfo(URL, dbname=name)
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def load_history(url):
    """Two synthetic suppliers: S-ONE kept 3 of 4 order lines, S-TWO 1 of 4."""
    with kb_db.connect(url, "kb") as conn:
        kb_db.migrate(conn, "kb")
        conn.execute("INSERT INTO documents (path, sha256, status) VALUES ('synthetic', 'synthetic', 'extracted')")
        for supplier, kept in (("S-ONE", 3), ("S-TWO", 1)):
            for i in range(4):
                values = {"supplier_id": supplier, "promised_date": "2030-01-10",
                          "actual_delivery_date": "2030-01-10" if i < kept else "2030-01-12"}
                for name, value in values.items():
                    conn.execute(
                        "INSERT INTO facts (fact_id, source_path, source_sha256, fact_type, subject, "
                        "attribute, value, extractor, confidence) "
                        "VALUES (%s, 'synthetic', 'synthetic', 'po_line', %s, %s, %s, 'test', 1)",
                        (f"{supplier}:{i}:{name}", Jsonb({"po_number": f"{supplier}-{i}", "po_line": "1"}),
                         name, Jsonb(value)))
        conn.commit()
        return conn.execute("SELECT * FROM current_facts").fetchall()


def test_the_persona_shows_the_score_of_the_named_history_and_a_cancellation_lowers_it(demo_db, tmp_path):
    facts, pack, log = load_history(demo_db), demo.load_pack(HERE), tmp_path / "log"
    card = demo.reset(demo_db, pack, DEADLINE, VALUES, log, history_supplier="S-ONE")[2]

    expected = reliability.card(reliability.history(facts), "S-ONE")
    assert card["score"] == expected["score"]
    assert card["supplier_id"] == pack["persona"]["persona_id"] and card["history_supplier_id"] == "S-ONE"
    cited = {s["fact_id"] for s in card["score"]["sources"] if "fact_id" in s}
    assert "S-ONE:3:actual_delivery_date" in cited and cited <= {f["fact_id"] for f in facts}

    [shown] = Lookahead(log).state()["suppliers"]
    assert shown["score"]["value"] == expected["score"]["value"]
    assert "did not make this offer" in shown["persona"] and "S-ONE" in shown["persona"]
    assert shown["flags"] == ["Scenario offer", "Real history: S-ONE"]

    after = reliability.update(card, "broken", "check-in", at=DEADLINE)
    contracts.validate_record(after)
    append_records(log, [after])
    [lowered] = Lookahead(log).state()["suppliers"]
    assert lowered["score"]["value"] < shown["score"]["value"]
    assert lowered["persona"] == shown["persona"] and lowered["flags"] == shown["flags"]


@pytest.mark.parametrize("loaded, reason", [(False, "no kb facts"), (True, "no delivery history")])
def test_a_missing_history_is_refused_and_keeps_the_old_plan(demo_db, tmp_path, loaded, reason):
    pack = demo.load_pack(HERE)
    demo.reset(demo_db, pack, DEADLINE, VALUES, tmp_path / "one")
    if loaded:
        load_history(demo_db)
    with plan_db.connect(demo_db) as conn:
        conn.execute("INSERT INTO goals (statement) VALUES ('old')")
        conn.commit()
    with pytest.raises(demo.DemoError, match=reason):
        demo.reset(demo_db, pack, DEADLINE, VALUES, tmp_path / "two", history_supplier="S-NONE")
    assert not (tmp_path / "two").exists()
    with plan_db.connect(demo_db) as conn:
        assert conn.execute("SELECT count(*) AS n FROM goals").fetchone()["n"] == 1
