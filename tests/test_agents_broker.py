"""The promise broker role (the LLM half) against a throwaway Postgres.

- the role (replay adapter, no key, no network) registers a watched promise and drafts a backup offer and a
  follow-up through MailTools; nothing is sent (dry-run mail).
- the grounded register tool: every number a promise carries (the supplier, the made time, the part, the quantity
  and the due time) comes from the cited kb statement, never from the model, and a call whose statement does not
  carry one is refused.
The ledger itself is in test_broker_ledger.py. Synthetic values only."""

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


def _registry(plan_conn, mail_env, *, statements=()):
    from mail.tools import MailTools
    s, mail_conn = mail_env
    mail = MailTools(s, mail_conn, actor="agent:broker")
    reg: list[dict] = []
    registry = broker.broker_registry(plan_conn, mail, registered=reg, statements=statements)
    return registry, reg


# ---- the role end to end -----------------------------------------------------

def test_broker_registers_a_promise_and_drafts_the_hedge_without_sending(plan_conn, mail_env):
    from mail.tools import MailTools
    s, mail_conn = mail_env
    mail = MailTools(s, mail_conn, actor="agent:broker")

    client = llm.ReplayClient(FIXTURES / "broker_hedge.json")
    # The statement the broker is briefing on: the register tool grounds the made time, supplier, part and the
    # quoted duration in it (and refuses a cited fact it does not know), so the promise must cite fact-1.
    said = Statement(kind="confirm", at=NOW.isoformat(), supplier_id="SUP-0028", part_id="PART-A",
                     fact_id="fact-1", text="loads the backup order within 4 h")
    result, registered = broker.run_broker(client, plan_conn, mail,
                                            prompt="A supplier promised to load the backup order within 4 h.",
                                            statements=[said])
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


# ---- the grounded register tool ----------------------------------------------

def test_register_grounds_made_time_quantity_and_due_in_the_cited_statement(plan_conn, mail_env):
    st = Statement(kind="confirm", at="2030-01-11T10:00:00+00:00", supplier_id="SUP-0028", part_id="PART-A",
                   fact_id="S-1", quantity="10 000", text="loads within 4 h")
    registry, reg = _registry(plan_conn, mail_env, statements=[st])
    out = registry.call("register_promise", {
        "promise_id": "PR-9", "what": "loads the backup order", "within": "within 4 h",
        "due": "2030-02-01T00:00:00+00:00", "quantity": 999, "next_check": "2099-01-01T00:00:00+00:00",
        "fallback": "call the supplier contact", "part_id": "PART-A", "sources": [{"fact_id": "S-1"}]})
    assert out.get("promise_id") == "PR-9"
    plan_conn.commit()
    row = plan_conn.execute("SELECT supplier_id, due, next_check, quantity FROM promises "
                            "WHERE promise_id = 'PR-9'").fetchone()
    # made time is the statement's 10:00, not the run clock; the model's due/next_check/quantity are ignored.
    assert row["due"] == datetime(2030, 1, 11, 14, 0, tzinfo=timezone.utc)
    assert row["next_check"] == datetime(2030, 1, 11, 14, 0, tzinfo=timezone.utc)
    assert row["quantity"] == 10000
    assert row["supplier_id"] == "SUP-0028"           # taken from the cited statement


def test_register_grounds_the_due_from_the_statements_due_time(plan_conn, mail_env):
    # A statement that gives an absolute due (no duration in its text): the promise's due is the statement's own
    # due_time, and a due the model passes is accepted only when it matches it.
    st = Statement(kind="delay", at="2030-01-11T10:00:00+00:00", supplier_id="SUP-0028", part_id="PART-A",
                   fact_id="S-2", due_time="2030-01-12T09:00:00+00:00")
    registry, reg = _registry(plan_conn, mail_env, statements=[st])
    out = registry.call("register_promise", {
        "promise_id": "PR-due", "what": "ships the order", "due": "2030-01-12T09:00:00+00:00",
        "fallback": "call the supplier contact", "sources": [{"fact_id": "S-2"}]})
    assert out.get("promise_id") == "PR-due"
    plan_conn.commit()
    row = plan_conn.execute("SELECT due FROM promises WHERE promise_id = 'PR-due'").fetchone()
    assert row["due"] == datetime(2030, 1, 12, 9, 0, tzinfo=timezone.utc)


def test_register_takes_the_supplier_from_the_statement_over_the_model(plan_conn, mail_env):
    # The model must not reassign a statement to another supplier: the cited statement's supplier wins.
    st = Statement(kind="confirm", at=NOW.isoformat(), supplier_id="SUP-0028", part_id="PART-A",
                   fact_id="S-1", text="loads within 4 h")
    registry, reg = _registry(plan_conn, mail_env, statements=[st])
    out = registry.call("register_promise", {
        "promise_id": "PR-2", "supplier_id": "SUP-WRONG", "what": "loads the backup order", "within": "within 4 h",
        "fallback": "call the supplier contact", "sources": [{"fact_id": "S-1"}]})
    assert out.get("promise_id") == "PR-2"
    plan_conn.commit()
    row = plan_conn.execute("SELECT supplier_id FROM promises WHERE promise_id = 'PR-2'").fetchone()
    assert row["supplier_id"] == "SUP-0028"


def test_register_takes_the_part_from_the_statement_over_the_model(plan_conn, mail_env):
    # A part the statement names wins over any the model passed (finding: the model's part_id won).
    st = Statement(kind="confirm", at=NOW.isoformat(), supplier_id="SUP-0028", part_id="PART-REAL",
                   fact_id="S-1", text="loads within 4 h")
    registry, reg = _registry(plan_conn, mail_env, statements=[st])
    out = registry.call("register_promise", {
        "promise_id": "PR-p", "what": "loads", "within": "within 4 h", "fallback": "call",
        "part_id": "PART-MADEUP", "sources": [{"fact_id": "S-1"}]})
    assert out.get("promise_id") == "PR-p"
    plan_conn.commit()
    row = plan_conn.execute("SELECT part_id FROM promises WHERE promise_id = 'PR-p'").fetchone()
    assert row["part_id"] == "PART-REAL"


def test_register_refuses_a_statement_with_no_supplier(plan_conn, mail_env):
    # On the reference facts no statement has a supplier; a promise cannot be watched against one, so it is refused.
    st = Statement(kind="confirm", at=NOW.isoformat(), supplier_id=None, part_id="PART-A",
                   fact_id="S-1", text="loads within 4 h")
    registry, reg = _registry(plan_conn, mail_env, statements=[st])
    out = registry.call("register_promise", {
        "promise_id": "PR-ns", "supplier_id": "SUP-GUESS", "what": "loads", "within": "within 4 h",
        "fallback": "call", "sources": [{"fact_id": "S-1"}]})
    assert "error" in out and reg == []
    assert plan_conn.execute("SELECT count(*) AS n FROM promises").fetchone()["n"] == 0


def test_register_refuses_a_statement_with_no_readable_time(plan_conn, mail_env):
    # No readable statement time (true of the reference facts until #171): the run clock must not stand in for it.
    st = Statement(kind="confirm", at=None, supplier_id="SUP-0028", part_id="PART-A",
                   fact_id="S-1", text="loads within 4 h")
    registry, reg = _registry(plan_conn, mail_env, statements=[st])
    out = registry.call("register_promise", {
        "promise_id": "PR-nt", "what": "loads", "within": "within 4 h", "fallback": "call",
        "sources": [{"fact_id": "S-1"}]})
    assert "error" in out and reg == []
    assert plan_conn.execute("SELECT count(*) AS n FROM promises").fetchone()["n"] == 0


def test_register_refuses_a_within_not_in_the_statement_text(plan_conn, mail_env):
    # A duration the statement does not hold is the model inventing a due time; it is refused, not saved.
    st = Statement(kind="confirm", at=NOW.isoformat(), supplier_id="SUP-0028", part_id="PART-A",
                   fact_id="S-1", text="loads within 4 h")
    registry, reg = _registry(plan_conn, mail_env, statements=[st])
    out = registry.call("register_promise", {
        "promise_id": "PR-iw", "what": "loads", "within": "within 900 h", "fallback": "call",
        "sources": [{"fact_id": "S-1"}]})
    assert "error" in out and reg == []
    assert plan_conn.execute("SELECT count(*) AS n FROM promises").fetchone()["n"] == 0


def test_register_refuses_an_absolute_due_not_in_the_statement(plan_conn, mail_env):
    # The statement has no duration and no due_time: an absolute due the model invents is refused.
    st = Statement(kind="delay", at=NOW.isoformat(), supplier_id="SUP-0028", part_id="PART-A", fact_id="S-1")
    registry, reg = _registry(plan_conn, mail_env, statements=[st])
    out = registry.call("register_promise", {
        "promise_id": "PR-id", "what": "ships", "due": "2030-06-01T00:00:00+00:00", "fallback": "call",
        "sources": [{"fact_id": "S-1"}]})
    assert "error" in out and reg == []
    assert plan_conn.execute("SELECT count(*) AS n FROM promises").fetchone()["n"] == 0


def test_register_refuses_an_invented_fact_id(plan_conn, mail_env):
    # A cited fact the kb does not hold is refused, so the model cannot register a promise on nothing.
    st = Statement(kind="confirm", at=NOW.isoformat(), supplier_id="SUP-A", part_id="PART-A",
                   fact_id="real", text="loads within 4 h")
    registry, reg = _registry(plan_conn, mail_env, statements=[st])
    out = registry.call("register_promise", {
        "promise_id": "PR-x", "what": "x", "due": "2030-02-01T00:00:00+00:00",
        "fallback": "call", "part_id": "PART-A", "sources": [{"fact_id": "no-such-fact"}]})
    assert "error" in out and reg == []
    assert plan_conn.execute("SELECT count(*) AS n FROM promises").fetchone()["n"] == 0


def test_register_refuses_a_promise_with_no_cited_fact(plan_conn, mail_env):
    registry, reg = _registry(plan_conn, mail_env)
    out = registry.call("register_promise", {"promise_id": "PR-x", "supplier_id": "SUP-A",
                                             "what": "x", "within": "within 4 h", "fallback": "call",
                                             "sources": []})
    assert "error" in out and reg == []


def test_a_reused_waiting_id_does_not_overwrite_another_promise(plan_conn, mail_env):
    # The model picks the id without seeing the open ones. A second statement that reuses a waiting id must not
    # replace the first promise (finding: a reused waiting id deleted the first, with no check-in).
    a = Statement(kind="confirm", at=NOW.isoformat(), supplier_id="SUP-0028", part_id="PART-A",
                  fact_id="S-a", text="loads within 4 h")
    b = Statement(kind="confirm", at=NOW.isoformat(), supplier_id="SUP-0028", part_id="PART-B",
                  fact_id="S-b", text="loads within 6 h")
    registry, reg = _registry(plan_conn, mail_env, statements=[a, b])
    first = registry.call("register_promise", {"promise_id": "P1", "what": "loads A", "within": "within 4 h",
                                               "fallback": "call", "sources": [{"fact_id": "S-a"}]})
    second = registry.call("register_promise", {"promise_id": "P1", "what": "loads B", "within": "within 6 h",
                                                "fallback": "call", "sources": [{"fact_id": "S-b"}]})
    assert first.get("promise_id") == "P1" and "error" in second
    plan_conn.commit()
    row = plan_conn.execute("SELECT what, sources FROM promises WHERE promise_id = 'P1'").fetchone()
    assert row["what"] == "loads A" and row["sources"] == [{"fact_id": "S-a"}]
    assert plan_conn.execute("SELECT count(*) AS n FROM promises").fetchone()["n"] == 1


def test_re_registering_the_same_statement_is_idempotent(plan_conn, mail_env):
    # Re-registering the same statement under its id is allowed: it is the same promise, not another one.
    st = Statement(kind="confirm", at=NOW.isoformat(), supplier_id="SUP-0028", part_id="PART-A",
                   fact_id="S-1", text="loads within 4 h")
    registry, reg = _registry(plan_conn, mail_env, statements=[st])
    args = {"promise_id": "P1", "what": "loads", "within": "within 4 h", "fallback": "call",
            "sources": [{"fact_id": "S-1"}]}
    assert registry.call("register_promise", dict(args)).get("promise_id") == "P1"
    assert registry.call("register_promise", dict(args)).get("promise_id") == "P1"
    plan_conn.commit()
    assert plan_conn.execute("SELECT count(*) AS n FROM promises").fetchone()["n"] == 1


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
    st = Statement(kind="confirm", at=NOW.isoformat(), supplier_id="SUP-A", part_id="PART-A",
                   fact_id="f", text="loads within 4 h")
    registry, reg = _registry(plan_conn, mail_env, statements=[st])
    bad = registry.call("register_promise", {"promise_id": "PR-bad", "what": "x", "within": "within 4 h",
                                             "fallback": "call", "decision_id": "NOPE",
                                             "sources": [{"fact_id": "f"}]})
    assert "error" in bad
    good = registry.call("register_promise", {"promise_id": "PR-good", "what": "x", "within": "within 4 h",
                                              "fallback": "call", "sources": [{"fact_id": "f"}]})
    assert good.get("promise_id") == "PR-good"
    plan_conn.commit()
    ids = [r["promise_id"] for r in plan_conn.execute(
        "SELECT promise_id FROM promises ORDER BY promise_id").fetchall()]
    assert ids == ["PR-good"] and [p["promise_id"] for p in reg] == ["PR-good"]
