from copy import deepcopy

import pytest

from agents.learner import publish_updates, supplier_updates
from sourcing.reliability import History, card


def test_settled_promises_update_the_supplier_card_once():
    original = card(History({}), "supplier-x", scenario_prior={"value": 0.8, "label": "example"})
    original_copy = deepcopy(original)
    broken = {"promise_id": "p1", "supplier_id": "supplier-x", "state": "broken",
              "broken_at": "2026-01-01T00:00:00Z", "sources": [{"fact_id": "f1"}]}
    updates = supplier_updates([original], [broken])
    assert len(updates) == 1
    assert updates[0]["score"]["value"] < original["score"]["value"]
    assert updates[0]["score"]["outcomes"][0]["sources"] == [{"fact_id": "f1"}]
    assert supplier_updates(updates, [broken]) == []
    assert original == original_copy


def test_publish_kept_outcome_and_ignore_unsettled_or_unknown():
    original = card(History({}), "supplier-x", scenario_prior={"value": 0.5, "label": "example"})
    class Timeline:
        def __init__(self):
            self.records = []
        def append(self, records):
            self.records.extend(records)
    timeline = Timeline()
    records = publish_updates(timeline, [original], [
        {"promise_id": "p1", "supplier_id": "supplier-x", "state": "proven"},
        {"promise_id": "p2", "supplier_id": "supplier-x", "state": "waiting"},
        {"promise_id": "p3", "supplier_id": "unknown", "state": "broken"},
    ])
    assert timeline.records == records
    assert records[0]["kind"] == "supplier"
    assert records[0]["score"]["value"] > original["score"]["value"]
    assert len(records[0]["score"]["outcomes"]) == 1


def test_duplicate_settled_promises_are_rejected():
    original = card(History({}), "supplier-x", scenario_prior={"value": 0.5, "label": "example"})
    p = {"promise_id": "p1", "supplier_id": "supplier-x", "state": "broken"}
    with pytest.raises(ValueError, match="duplicate settled promise"):
        supplier_updates([original], [p, p])


def test_email_classification_fails_closed():
    from agents.learner import classify_email
    from agents.llm import ReplayClient

    client = ReplayClient([
        {"text": '{"outcome":"broken"}'},
        {"text": '{"outcome":"unclear"}'},
        {"text": '{"outcome":"broken", "extra":true}'},
        {"text": 'not json'},
    ])
    assert classify_email(client, "The shipment has been cancelled.") == "broken"
    assert classify_email(client, "We are checking.") is None
    assert classify_email(client, "ambiguous") is None
    assert classify_email(client, "ambiguous") is None


def test_ledger_updates_real_timeline_and_is_idempotent(tmp_path):
    from datetime import datetime, timezone
    from agents.learner import learn_from_ledger
    from tracker.lookahead import Lookahead

    timeline = Lookahead(tmp_path / "log.jsonl")
    original = card(History({}), "supplier-x", scenario_prior={"value": 0.8, "label": "example"})
    timeline.append([original])

    class Conn:
        def execute(self, sql):
            assert "FROM promises" in sql
            return self
        def fetchall(self):
            return [{"promise_id": "p1", "supplier_id": "supplier-x", "state": "broken",
                     "broken_at": datetime(2026, 1, 1, tzinfo=timezone.utc), "sources": [{"fact_id": "f1"}]}]

    learn_from_ledger(Conn(), timeline)
    assert timeline.state()["suppliers"][0]["score"]["value"] < original["score"]["value"]
    assert learn_from_ledger(Conn(), timeline) == []


def test_broken_promise_from_plan_database_reaches_timeline(tmp_path):
    import os
    import uuid
    from datetime import datetime, timezone
    from psycopg import sql
    from agents import broker
    from agents.learner import learn_from_ledger
    from plan import db
    from sourcing.promises import Statement, promise_from_statement
    from tracker.lookahead import Lookahead

    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    schema = f"plan_test_{uuid.uuid4().hex[:8]}"
    conn = db.connect(url, schema)
    try:
        db.migrate(conn, schema)
        promise = promise_from_statement(promise_id="p1", supplier_id="supplier-x", what="deliver",
                                         made_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
                                         within="within 4 h", fallback="follow up",
                                         sources=[{"fact_id": "made"}])
        broker.save_promise(conn, promise)
        updates = broker.apply_ledger(conn, [Statement(kind="cancel", at="2030-01-01T01:00:00+00:00",
                                                       supplier_id="supplier-x", fact_id="cancel")])
        assert updates[0]["state"] == "broken"
        timeline = Lookahead(tmp_path / "log.jsonl")
        original = card(History({}), "supplier-x", scenario_prior={"value": 0.8, "label": "example"})
        timeline.append([original])
        learn_from_ledger(conn, timeline)
        assert timeline.state()["suppliers"][0]["score"]["value"] < original["score"]["value"]
        assert learn_from_ledger(conn, timeline) == []
    finally:
        conn.rollback()
        conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
        conn.commit()
        conn.close()
