"""kb against a real Postgres, in a throwaway schema that is dropped afterwards.

Runs when TEST_DATABASE_URL is set, or with KB_TEST_USE_DEV_DB=1 (uses the tracker's database from .env /
secrets/db.env, still only inside its own temporary schema). Synthetic data only."""

import hashlib
import json
import os
import uuid

import pytest
from psycopg import sql

from conftest import make_pdf
from kb import db, reconcile, rules
from kb.ingest import process_file
from kb.tabular import signature

URL = os.environ.get("TEST_DATABASE_URL")
if not URL and os.environ.get("KB_TEST_USE_DEV_DB") == "1":
    from kb.config import load_settings
    URL = load_settings().database_url
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL (or KB_TEST_USE_DEV_DB=1) not set")

SEED = """
rules:
  - rule_id: received
    question: How many arrived?
    key: [note]
    candidates:
      - {label: paper, fact_type: note_line, attribute: final_qty}
      - {label: erp, fact_type: receipt, attribute: qty}
    compare: number
    decide: preference
    severity: high
    explanation: "We believe the {winner} ({winner_value})."
settings:
  confidence_map: {high: 0.9, medium: 0.75, low: 0.4}
"""
HEADER = ["rid", "note", "qty"]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def env(tmp_path):
    schema = f"kb_test_{uuid.uuid4().hex[:8]}"
    conn = db.connect(URL, schema)
    db.migrate(conn, schema)
    seed = tmp_path / "seed.yaml"
    seed.write_text(SEED)
    rules.seed(conn, seed)
    conn.commit()
    structured = tmp_path / "structured"
    (structured / "docs").mkdir(parents=True)
    conn.execute("INSERT INTO schema_maps (signature, format, mapping, origin) VALUES (%s, 'csv', %s, 'test')", (
        signature(HEADER), json.dumps({"signature": signature(HEADER), "fact_type": "receipt", "subject": ["rid"],
                                       "columns": {"rid": {"field": "rid", "type": "text"},
                                                   "note": {"field": "note", "type": "text"},
                                                   "qty": {"field": "qty", "type": "int"}}})))
    conn.commit()
    try:
        yield conn, structured
    finally:
        conn.rollback()
        conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        conn.commit()
        conn.close()


def envelope(path: str, data: bytes, note: str, qty: int) -> dict:
    return {"source": {"path": path, "sha256": sha(data)}, "doc_kind": "delivery_note", "spec_version": "1",
            "extracted_by": "w1", "facts": [
                {"fact_type": "note_line", "subject": {"note": note, "line": 1}, "attribute": "final_qty",
                 "value": qty, "evidence": str(qty), "confidence": "high"}]}


def open_issues(conn) -> dict:
    return {r["kind"]: r for r in conn.execute("SELECT * FROM issues WHERE state = 'open'")}


def test_end_to_end_ingest_rules_and_changes(env):
    conn, structured = env
    csv_v1 = b"rid,note,qty\nR1,N1,100\nR2,N2,7\n"
    assert process_file(conn, "erp/receipts.csv", sha(csv_v1), csv_v1, structured) == "parsed"

    scan = make_pdf(["note N1"])
    assert process_file(conn, "scans/n1.pdf", sha(scan), scan, structured) == "queued"
    conn.commit()
    first = reconcile.run(conn, "test")
    issues = open_issues(conn)
    assert set(issues) == {"pending_extraction"}
    assert first["believed"] == 2

    (structured / "docs" / f"{sha(scan)}.json").write_text(json.dumps(envelope("scans/n1.pdf", scan, "N1", 10)))
    assert process_file(conn, "scans/n1.pdf", sha(scan), scan, structured) == "loaded"
    conn.commit()
    second = reconcile.run(conn, "test")
    issues = open_issues(conn)
    assert set(issues) == {"contradiction"}
    assert issues["contradiction"]["payload"]["believed"] == 10
    assert [c for c in second["changes"]["resolved"]]

    # Rule change: prefer the ERP. Preview shows the change without applying it.
    draft = rules.draft(conn, rules.active_version(conn), "tester", "prefer erp")
    rule, _ = rules.load_rule_set(conn, draft)
    rule = rule[0]
    rule["candidates"].reverse()
    rules.put_rule(conn, draft, rule)
    conn.commit()
    preview = reconcile.run(conn, "rule_preview", version=draft, preview=True)
    conn.rollback()
    assert len(preview["changes"]["changed"]) == 1
    believed = conn.execute("SELECT value, rule_set_version FROM believed WHERE key = %s",
                            (json.dumps({"note": "N1"}),)).fetchone()
    assert believed["value"] == 10 and believed["rule_set_version"] == 1

    rules.activate(conn, draft)
    conn.commit()
    reconcile.run(conn, "rule_change")
    believed = conn.execute("SELECT value, rule_set_version, explanation FROM believed WHERE key = %s",
                            (json.dumps({"note": "N1"}),)).fetchone()
    assert believed["value"] == 100 and believed["rule_set_version"] == draft
    assert "erp" in believed["explanation"]
    with pytest.raises(rules.RuleError):
        rules.put_rule(conn, draft, rule)
    conn.rollback()

    # The ERP export changes format: unknown header -> visible issue, old facts no longer count.
    csv_v2 = b"rid;delivery note;qty\nR1;N1;100\n"
    assert process_file(conn, "erp/receipts.csv", sha(csv_v2), csv_v2, structured) == "needs_schema_map"
    conn.commit()
    third = reconcile.run(conn, "test")
    issues = open_issues(conn)
    assert set(issues) == {"schema_map_needed"}
    assert third["changes"]["resolved"]
    events = conn.execute("SELECT change, count(*) AS n FROM issue_events GROUP BY change").fetchall()
    assert {e["change"] for e in events} >= {"new", "changed", "resolved"}


def test_fact_events_and_current_facts_view(env):
    conn, structured = env
    v1 = b"rid,note,qty\nR1,N1,100\n"
    process_file(conn, "erp/receipts.csv", sha(v1), v1, structured)
    process_file(conn, "erp/receipts.csv", sha(v1), v1, structured)          # same version again: no event
    v2 = b"rid,note,qty\nR1,N1,90\n"
    process_file(conn, "erp/receipts.csv", sha(v2), v2, structured)
    from kb.ingest import remove_path
    remove_path(conn, "erp/receipts.csv")
    conn.commit()
    rows = conn.execute("SELECT change, sha256, previous_sha256, n_facts FROM fact_events ORDER BY id").fetchall()
    assert [r["change"] for r in rows] == ["facts_added", "facts_replaced", "facts_removed"]
    assert rows[1]["sha256"] == sha(v2) and rows[1]["previous_sha256"] == [sha(v1)]
    assert rows[2]["n_facts"] == 0
    process_file(conn, "erp/receipts.csv", sha(v2), v2, structured)
    conn.commit()
    view = conn.execute("SELECT path, sha256, fact_type, subject, attribute, value FROM current_facts "
                        "WHERE attribute = 'qty'").fetchall()
    assert view == [{"path": "erp/receipts.csv", "sha256": sha(v2), "fact_type": "receipt",
                     "subject": {"rid": "R1"}, "attribute": "qty", "value": 90}]
