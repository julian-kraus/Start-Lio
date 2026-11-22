"""Fact queries: the pure helpers, and load() against a throwaway schema with synthetic rows."""

import os
import uuid

import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from kb import db
from sourcing import facts


@pytest.mark.parametrize("text, value", [("10 000", 10000), ("10,000", 10000), ("2.5", 2.5), (7.0, 7),
                                         ("1,5", None), ("about 5", None), (True, None), (None, None)])
def test_numbers_are_parsed_from_the_text(text, value):
    assert facts.number(text) == value


def test_ids_are_unique_and_point_at_the_original_fact():
    assert facts.ids(["derived:a", "b"], ["a", "c"]) == ["a", "b", "c"]


def test_newest_fact_wins_inside_a_row():
    base = {"path": "f", "fact_type": "part", "subject": {"id": "x"}, "attribute": "qty", "confidence": 0.9}
    found = facts.Facts([{**base, "fact_id": "new", "value": 2, "as_of": "2030-01-02"},
                         {**base, "fact_id": "old", "value": 1, "as_of": "2030-01-01"}]).rows("part")[0]
    assert (found.get("qty"), found.ids("qty")) == (2, ["new"])


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")
def test_load_reads_current_facts_believed_and_open_issues():
    schema = f"kb_test_{uuid.uuid4().hex[:8]}"
    conn = db.connect(os.environ["TEST_DATABASE_URL"], schema)
    try:
        db.migrate(conn, schema)
        for fact_id, valid in (("live", True), ("stale", False)):
            conn.execute(
                "INSERT INTO facts (fact_id, fact_type, subject, attribute, value, extractor, confidence, "
                "source_path, source_sha256, valid) VALUES (%s, 'part', %s, 'qty', %s, 't', 1, 'f.csv', 's', %s)",
                (fact_id, Jsonb({"id": "X"}), Jsonb(5), valid))
        conn.execute("INSERT INTO believed (rule_id, key, rule_set_version, value, winning_fact_ids) "
                     "VALUES ('r', %s, 1, %s, %s)", (Jsonb({"id": "X"}), Jsonb(5), ["live"]))
        for key, state in (("a", "open"), ("b", "resolved")):
            conn.execute("INSERT INTO issues (issue_key, kind, severity, state, title, payload, payload_hash) "
                         "VALUES (%s, %s, 'low', %s, 't', %s, 'h')", (key, key, state, Jsonb({})))
        conn.commit()
        loaded = facts.load(conn, schema)
        assert list(loaded.by_id) == ["live"]
        assert loaded.rows("part", id="X")[0].get("qty") == 5
        assert loaded.believed("r", id="X")[0]["value"] == 5
        assert [i["kind"] for i in loaded.issues] == ["a"]
    finally:
        conn.rollback()
        conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
        conn.commit()
        conn.close()
