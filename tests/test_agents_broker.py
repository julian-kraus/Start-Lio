"""The promise broker and its ledger against a throwaway Postgres.

- the broker role (replay adapter, no key, no network) registers a watched promise and drafts a backup offer and
  a follow-up through MailTools; nothing is sent (dry-run mail).
- the ledger folds supplier statements onto the open promises, proving or breaking them, and keeps every open
  promise watchable.
Synthetic values only."""

import dataclasses
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from psycopg import sql

from agents import broker, llm
from sourcing import promises
from sourcing.promises import Statement

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "replay"
URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")

NOW = datetime(2030, 1, 11, 14, 0, tzinfo=timezone.utc)
OWN = "desk@example.com"
SUP_ADDR = "anna@suppliers.example.net"


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


@pytest.fixture
def mail_env(tmp_path):
    from mail import contacts, db as maildb
    from mail.config import load_settings
    schema = f"mail_test_{uuid.uuid4().hex[:8]}"
    base = load_settings(load_files=False)
    s = dataclasses.replace(base, database_url=URL, schema=schema, address=OWN, display_name="Desk",
                            send_mode="dry_run", sender_registered=False, test_addresses=(),
                            data_dir=tmp_path / "mail_data", tracker_data_dir=tmp_path / "data",
                            kb_schema=f"nokb_{uuid.uuid4().hex[:6]}", min_gap_per_recipient=0)
    conn = maildb.connect(URL, schema)
    maildb.migrate(conn, schema)
    conn.commit()
    prof = tmp_path / "data" / "current" / "suppliers" / "SUP-0028_Test" / "profile.csv"
    prof.parent.mkdir(parents=True)
    prof.write_text("supplier_id;name;contact_name;contact_email;preferred_language\n"
                    f"SUP-0028;Test Karton;Anna;{SUP_ADDR};pl\n")
    contacts.sync(conn, s)
    conn.commit()
    yield s, conn
    conn.rollback()
    conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
    conn.commit()
    conn.close()


# ---- the broker role ---------------------------------------------------------

def test_broker_registers_a_promise_and_drafts_the_hedge_without_sending(plan_conn, mail_env):
    from mail.tools import MailTools
    s, mail_conn = mail_env
    mail = MailTools(s, mail_conn, actor="agent:broker")

    client = llm.ReplayClient(FIXTURES / "broker_hedge.json")
    result, registered = broker.run_broker(client, plan_conn, mail,
                                            prompt="A supplier promised to load the backup order within 4 h.",
                                            now=NOW)
    plan_conn.commit()

    assert result.ok
    assert result.calls == ["register_promise", "draft_email", "draft_email"]
    assert [p["promise_id"] for p in registered] == ["PR-1"]

    # The promise is saved, watched (a check and a fallback), and due 4 h after the statement, computed in code.
    row = plan_conn.execute("SELECT supplier_id, what, due, next_check, fallback, state, sources "
                            "FROM promises WHERE promise_id = 'PR-1'").fetchone()
    assert row["state"] == "waiting" and row["fallback"] == "call the supplier contact"
    assert row["due"] == datetime(2030, 1, 11, 18, 0, tzinfo=timezone.utc)
    assert row["next_check"] == datetime(2030, 1, 11, 18, 0, tzinfo=timezone.utc)
    assert row["sources"] == [{"fact_id": "fact-1"}]
    assert promises.watch_gaps(broker.open_promises(plan_conn)) == []

    # Two drafts stored (a backup offer and a follow-up); nothing sent.
    drafts = mail_conn.execute("SELECT subject, status FROM drafts ORDER BY created_at").fetchall()
    assert len(drafts) == 2
    assert [d["subject"].split(" [")[0] for d in drafts] == [
        "Zapytanie: oferta zapasowa", "Przypomnienie: potwierdzenie zaladunku"]
    assert all(d["status"] == "draft" for d in drafts)
    assert mail_conn.execute("SELECT count(*) AS n FROM sends").fetchone()["n"] == 0


# ---- the ledger --------------------------------------------------------------

def _save_waiting(plan_conn, promise_id="PR-1", supplier_id="SUP-A", part_id="PART-A"):
    p = promises.promise_from_statement(promise_id=promise_id, supplier_id=supplier_id, part_id=part_id,
                                        what="loads the backup order", made_at=NOW, within="within 4 h",
                                        fallback="call the supplier contact", sources=[{"fact_id": "fact-made"}])
    broker.save_promise(plan_conn, p)
    plan_conn.commit()
    return p


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


# ---- the ledger: clock on reload, lateness, isolation -------------------------

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


# ---- the broker role: grounded numbers, no reopening, isolation ---------------

def _registry(plan_conn, mail_env, *, statements=()):
    from mail.tools import MailTools
    s, mail_conn = mail_env
    mail = MailTools(s, mail_conn, actor="agent:broker")
    reg: list[dict] = []
    registry = broker.broker_registry(plan_conn, mail, now_iso=NOW.isoformat(), registered=reg,
                                      statements=statements)
    return registry, reg


def test_register_grounds_made_time_and_quantity_in_the_cited_statement(plan_conn, mail_env):
    st = Statement(kind="confirm", at="2030-01-11T10:00:00+00:00", supplier_id="SUP-0028", part_id="PART-A",
                   fact_id="S-1", quantity="10 000")
    registry, reg = _registry(plan_conn, mail_env, statements=[st])
    out = registry.call("register_promise", {
        "promise_id": "PR-9", "what": "loads the backup order", "within": "within 4 h",
        "due": "2030-02-01T00:00:00+00:00", "quantity": 999, "next_check": "2099-01-01T00:00:00+00:00",
        "fallback": "call the supplier contact", "part_id": "PART-A", "sources": [{"fact_id": "S-1"}]})
    assert out.get("promise_id") == "PR-9"
    plan_conn.commit()
    row = plan_conn.execute("SELECT supplier_id, due, next_check, quantity FROM promises "
                            "WHERE promise_id = 'PR-9'").fetchone()
    # made time is the statement's 10:00, not the run's 14:00; the model's due/next_check/quantity are ignored.
    assert row["due"] == datetime(2030, 1, 11, 14, 0, tzinfo=timezone.utc)
    assert row["next_check"] == datetime(2030, 1, 11, 14, 0, tzinfo=timezone.utc)
    assert row["quantity"] == 10000
    assert row["supplier_id"] == "SUP-0028"           # taken from the cited statement


def test_register_refuses_a_promise_with_no_cited_fact(plan_conn, mail_env):
    registry, reg = _registry(plan_conn, mail_env)
    out = registry.call("register_promise", {"promise_id": "PR-x", "supplier_id": "SUP-A",
                                             "what": "x", "within": "within 4 h", "fallback": "call",
                                             "sources": []})
    assert "error" in out and reg == []


def test_register_does_not_reopen_a_settled_promise(plan_conn, mail_env):
    p = promises.promise_from_statement(promise_id="PR-7", supplier_id="SUP-A", what="x", made_at=NOW,
                                        within="within 4 h", fallback="call", sources=[{"fact_id": "f"}])
    broker.save_promise(plan_conn, p)
    plan_conn.execute("UPDATE promises SET state = 'broken', broken_at = now() WHERE promise_id = 'PR-7'")
    plan_conn.commit()
    registry, reg = _registry(plan_conn, mail_env)
    out = registry.call("register_promise", {"promise_id": "PR-7", "supplier_id": "SUP-A", "what": "x",
                                             "within": "within 4 h", "fallback": "call",
                                             "sources": [{"fact_id": "f"}]})
    assert "error" in out and reg == []
    row = plan_conn.execute("SELECT state, broken_at FROM promises WHERE promise_id = 'PR-7'").fetchone()
    assert row["state"] == "broken" and row["broken_at"] is not None


def test_one_bad_register_call_does_not_lose_the_good_ones(plan_conn, mail_env):
    registry, reg = _registry(plan_conn, mail_env)
    bad = registry.call("register_promise", {"promise_id": "PR-bad", "supplier_id": "SUP-A", "what": "x",
                                             "within": "within 4 h", "fallback": "call", "decision_id": "NOPE",
                                             "sources": [{"fact_id": "f"}]})
    assert "error" in bad
    good = registry.call("register_promise", {"promise_id": "PR-good", "supplier_id": "SUP-A", "what": "x",
                                              "within": "within 4 h", "fallback": "call",
                                              "sources": [{"fact_id": "f"}]})
    assert good.get("promise_id") == "PR-good"
    plan_conn.commit()
    ids = [r["promise_id"] for r in plan_conn.execute(
        "SELECT promise_id FROM promises ORDER BY promise_id").fetchall()]
    assert ids == ["PR-good"] and [p["promise_id"] for p in reg] == ["PR-good"]
