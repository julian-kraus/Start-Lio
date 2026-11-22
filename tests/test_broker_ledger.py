"""The promise ledger behind the broker (the deterministic half) against a throwaway Postgres.

- reading the kb's supplier statements (`statements_from_rows`, `read_statements`): the contract shape, the
  supplier, the statement time (never the kb load time), and the grouped fact ids. No email parser in agents/.
- folding statements onto the open promises (`apply_ledger`): proving, breaking, lateness, the clock on reload,
  and isolating a bad row.
The LLM role is in test_agents_broker.py. Synthetic values only."""

import os
import uuid
from datetime import datetime, timezone

import pytest
from psycopg import sql

from agents import broker
from sourcing import promises
from sourcing.promises import Statement

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")

NOW = datetime(2030, 1, 11, 14, 0, tzinfo=timezone.utc)


@pytest.fixture
def plan_conn():
    from plan import db
    schema = f"plan_test_{uuid.uuid4().hex[:8]}"
    conn = db.connect(URL, schema)
    db.migrate(conn, schema)
    conn.commit()
    yield conn
    conn.rollback()
    conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
    conn.commit()
    conn.close()


def _save_waiting(plan_conn, promise_id="PR-1", supplier_id="SUP-A", part_id="PART-A"):
    p = promises.promise_from_statement(promise_id=promise_id, supplier_id=supplier_id, part_id=part_id,
                                        what="loads the backup order", made_at=NOW, within="within 4 h",
                                        fallback="call the supplier contact", sources=[{"fact_id": "fact-made"}])
    broker.save_promise(plan_conn, p)
    plan_conn.commit()
    return p


# ---- reading kb supplier statements (no email parser in agents/) --------------

def _row(fact_id, attribute, value, *, statement_no="ST-1", material_id="PART-A", supplier_id="SUP-A",
         persona_id=None, path="mail/a.eml", sha256="sha-a", as_of="2030-01-11T17:00:00+00:00"):
    mail = {"supplier_id": supplier_id, "persona_id": persona_id}
    return {"fact_type": "supplier_statement", "fact_id": fact_id, "attribute": attribute, "value": value,
            "subject": {"statement_no": statement_no}, "locator": {"mail": mail},
            "path": path, "sha256": sha256, "as_of": as_of, "observed_at": None}


def test_statements_are_read_from_kb_facts_not_parsed_from_email():
    # The contract shape: statement_no is the subject; material_id, kind and value are attributes; the supplier
    # is in locator.mail. A po_line row is ignored.
    rows = [
        _row("f-kind", "kind", "dispatch_info"),
        _row("f-mat", "material_id", "PART-A"),
        _row("f-val", "value", "Loaded, 200 units."),
        {"fact_type": "po_line", "fact_id": "x", "attribute": "qty", "value": 200, "subject": {"po": "P1"}},
    ]
    [st] = broker.statements_from_rows(rows)
    assert st.kind == "dispatch_info" and st.part_id == "PART-A" and st.supplier_id == "SUP-A"
    assert st.text == "Loaded, 200 units." and st.fact_id == "f-kind"
    assert st.at == "2030-01-11T17:00:00+00:00"


def test_promise_kind_and_due_time_are_read_from_the_contract_attributes():
    # A statement classified by promise_kind (no `kind` attribute) is not dropped, and its quantity and due_time
    # come through verbatim (finding: email promises are dropped).
    rows = [
        _row("f-pk", "promise_kind", "delay"),
        _row("f-mat", "material_id", "PART-A"),
        _row("f-q", "quantity", "10 000"),
        _row("f-due", "due_time", "2030-01-12T09:00:00+00:00"),
    ]
    [st] = broker.statements_from_rows(rows)
    assert st.kind == "delay" and st.part_id == "PART-A"
    assert st.quantity == "10 000" and st.due_time == "2030-01-12T09:00:00+00:00"
    assert st.fact_id == "f-pk"                       # cites the classifying row
    assert promises.OUTCOMES["delay"] == promises.BROKEN


def test_the_persona_id_stands_in_for_a_missing_supplier_id():
    # Scenario persona mail leaves locator.mail.supplier_id null and carries the identity in persona_id.
    rows = [_row("f-kind", "kind", "on_hold", supplier_id=None, persona_id="PERSONA-7"),
            _row("f-mat", "material_id", "PART-A", supplier_id=None, persona_id="PERSONA-7")]
    [st] = broker.statements_from_rows(rows)
    assert st.supplier_id == "PERSONA-7"


def test_two_emails_that_reuse_a_statement_number_do_not_merge():
    # Same statement_no in two different documents must stay two statements, each citing its own fact, so a proof
    # does not cite a hold's evidence (finding: different emails merge and cite the wrong fact).
    rows = [
        _row("a-kind", "kind", "on_hold", path="mail/a.eml", sha256="sha-a"),
        _row("a-mat", "material_id", "PART-A", path="mail/a.eml", sha256="sha-a"),
        _row("b-kind", "kind", "dispatch_info", path="mail/b.eml", sha256="sha-b"),
        _row("b-mat", "material_id", "PART-A", path="mail/b.eml", sha256="sha-b"),
    ]
    sts = sorted(broker.statements_from_rows(rows), key=lambda s: s.kind)
    assert [s.kind for s in sts] == ["dispatch_info", "on_hold"]
    assert {s.fact_id for s in sts} == {"a-kind", "b-kind"}


def test_the_kb_load_time_is_not_used_as_the_statement_time():
    # as_of null (true of the reference facts): the statement time must not fall back to observed_at (today's kb
    # load), which would break a promise made before the load. With no document or mail time either, it is unknown
    # (finding: the kb load time was used as the statement time).
    rows = [_row("f-kind", "kind", "on_hold", as_of=None), _row("f-mat", "material_id", "PART-A", as_of=None)]
    [st] = broker.statements_from_rows(rows)
    assert st.at is None
    p = promises.promise_from_statement(promise_id="PR-1", supplier_id="SUP-A", part_id="PART-A", what="x",
                                        made_at=NOW, within="within 4 h", fallback="call",
                                        sources=[{"fact_id": "made"}])
    assert promises.reconcile([p], [st]) == []                 # an unknown time settles nothing
    assert promises.review([p], [st])[0]["reason"] == "unreadable_time"


def test_a_mail_date_stands_in_when_as_of_is_missing():
    # When the supplier-stated time is missing but the mail carries a date, that date is the statement time.
    rows = [{"fact_type": "supplier_statement", "fact_id": "f-kind", "attribute": "kind", "value": "dispatch_info",
             "subject": {"statement_no": "ST-1"}, "path": "mail/a.eml", "sha256": "sha-a", "as_of": None,
             "locator": {"mail": {"supplier_id": "SUP-A", "date_header": "2030-01-11T17:00:00+00:00"}}}]
    [st] = broker.statements_from_rows(rows)
    assert st.at == "2030-01-11T17:00:00+00:00"


def test_a_statement_keeps_every_grouped_fact_id():
    # The grouped statement carries all its fact ids, so the ledger recognises it as a promise's maker whichever
    # fact was cited (finding: citing another fact of the same email re-opened the self-proof).
    rows = [_row("f-kind", "kind", "dispatch_info"), _row("f-val", "value", "Loaded."),
            _row("f-mat", "material_id", "PART-A")]
    [st] = broker.statements_from_rows(rows)
    assert st.fact_id == "f-kind"
    assert st.all_fact_ids() == {"f-kind", "f-val", "f-mat"}


class _StubConn:
    def __init__(self, rows):
        self._rows = rows
        self.sql = None

    def execute(self, sql, *args):
        self.sql = sql
        return self

    def fetchall(self):
        return self._rows


def test_read_statements_selects_the_columns_it_needs_and_maps_them():
    rows = [_row("f-kind", "kind", "dispatch_info"), _row("f-mat", "material_id", "PART-A")]
    conn = _StubConn(rows)
    [st] = broker.read_statements(conn)
    assert st.kind == "dispatch_info" and st.part_id == "PART-A" and st.supplier_id == "SUP-A"
    # the query must pull locator (for the supplier) and path/sha256 (to group by document)
    for col in ("locator", "path", "sha256"):
        assert col in conn.sql


# ---- folding statements onto the open promises --------------------------------

def test_ledger_proves_a_promise_from_a_dispatch_statement(plan_conn):
    _save_waiting(plan_conn)
    st = Statement(kind="dispatch_info", at="2030-01-11T17:00:00+00:00", supplier_id="SUP-A",
                   part_id="PART-A", fact_id="fact-9")
    updates = broker.apply_ledger(plan_conn, [st])
    plan_conn.commit()
    assert [u["state"] for u in updates] == ["proven"]

    row = plan_conn.execute("SELECT state, proven_at, sources FROM promises WHERE promise_id = 'PR-1'").fetchone()
    assert row["state"] == "proven"
    assert row["proven_at"] == datetime(2030, 1, 11, 17, 0, tzinfo=timezone.utc)
    assert row["sources"][-1] == {"fact_id": "fact-9"}
    chk = plan_conn.execute("SELECT outcome, evidence FROM checkins WHERE promise_id = 'PR-1'").fetchone()
    assert chk["outcome"] == "proven" and chk["evidence"] == [{"fact_id": "fact-9"}]


def test_ledger_breaks_a_promise_and_leaves_others_watchable(plan_conn):
    _save_waiting(plan_conn, promise_id="PR-broken", supplier_id="SUP-A", part_id="PART-A")
    _save_waiting(plan_conn, promise_id="PR-open", supplier_id="SUP-B", part_id="PART-B")
    st = Statement(kind="on_hold", at="2030-01-11T16:00:00+00:00", supplier_id="SUP-A",
                   part_id="PART-A", fact_id="fact-hold")
    updates = broker.apply_ledger(plan_conn, [st])
    plan_conn.commit()
    assert [(u["promise_id"], u["state"]) for u in updates] == [("PR-broken", "broken")]

    broken = plan_conn.execute("SELECT state, broken_at FROM promises WHERE promise_id = 'PR-broken'").fetchone()
    assert broken["state"] == "broken" and broken["broken_at"] == datetime(2030, 1, 11, 16, 0, tzinfo=timezone.utc)
    # The untouched promise is still waiting, still has a check and a fallback.
    assert promises.watch_gaps(broker.open_promises(plan_conn)) == []
    still = plan_conn.execute("SELECT state FROM promises WHERE promise_id = 'PR-open'").fetchone()
    assert still["state"] == "waiting"


def test_the_statement_that_made_a_promise_does_not_prove_it_through_the_ledger(plan_conn):
    # The demo path: a confirm makes the promise, a later cancellation breaks it. read_statements returns every
    # current statement, so the citing confirm (fact-made) is in the run; it must not settle its own promise.
    _save_waiting(plan_conn)                                      # PR-1 rests on fact-made
    assert broker.open_promises(plan_conn)[0]["sources"] == [{"fact_id": "fact-made"}]
    said = Statement(kind="confirm", at=NOW.isoformat(), supplier_id="SUP-A", part_id="PART-A", fact_id="fact-made")
    cancel = Statement(kind="cancel", at="2030-01-11T19:00:00+00:00", supplier_id="SUP-A",
                       part_id="PART-A", fact_id="fact-cancel")
    updates = broker.apply_ledger(plan_conn, [said, cancel])
    plan_conn.commit()
    assert [(u["promise_id"], u["state"]) for u in updates] == [("PR-1", "broken")]
    row = plan_conn.execute("SELECT state FROM promises WHERE promise_id = 'PR-1'").fetchone()
    assert row["state"] == "broken"


def test_the_ledger_clock_survives_a_reload(plan_conn):
    # A promise made in the past: its made time is persisted (there is no made_at column) so after a reload the
    # ledger anchors on it, not on the database's wall clock (finding: the stored ledger uses the wrong clock).
    made = "2020-03-01T09:00:00+00:00"
    p = promises.promise_from_statement(promise_id="PR-old", supplier_id="SUP-A", part_id="PART-A",
                                        what="loads the backup order", made_at=made, within="within 4 h",
                                        fallback="call the supplier contact", sources=[{"fact_id": "f0"}])
    broker.save_promise(plan_conn, p)
    plan_conn.commit()
    stored = plan_conn.execute("SELECT created_at FROM promises WHERE promise_id = 'PR-old'").fetchone()
    assert stored["created_at"] == datetime(2020, 3, 1, 9, 0, tzinfo=timezone.utc)

    st = Statement(kind="on_hold", at="2020-03-01T12:00:00+00:00", supplier_id="SUP-A", part_id="PART-A",
                   fact_id="f-hold")
    updates = broker.apply_ledger(plan_conn, [st])
    plan_conn.commit()
    assert [u["state"] for u in updates] == ["broken"]


def test_a_late_proof_is_recorded_as_late(plan_conn):
    _save_waiting(plan_conn)                       # due 2030-01-11 18:00
    st = Statement(kind="dispatch_info", at="2030-01-13T17:00:00+00:00", supplier_id="SUP-A",
                   part_id="PART-A", fact_id="f-late")
    updates = broker.apply_ledger(plan_conn, [st])
    plan_conn.commit()
    assert updates[0]["state"] == "proven" and updates[0]["late"] is True
    chk = plan_conn.execute("SELECT outcome FROM checkins WHERE promise_id = 'PR-1'").fetchone()
    assert chk["outcome"] == "late"


def test_an_unreadable_statement_time_does_not_stop_the_ledger(plan_conn):
    _save_waiting(plan_conn)
    good = Statement(kind="dispatch_info", at="2030-01-11T17:00:00+00:00", supplier_id="SUP-A",
                     part_id="PART-A", fact_id="f-good")
    bad = Statement(kind="handling_note", at="2030-01-11", supplier_id="SUP-A", part_id="PART-A", fact_id="f-bad")
    updates = broker.apply_ledger(plan_conn, [bad, good])      # bad is informational, unreadable time
    plan_conn.commit()
    assert [u["promise_id"] for u in updates] == ["PR-1"] and updates[0]["state"] == "proven"
