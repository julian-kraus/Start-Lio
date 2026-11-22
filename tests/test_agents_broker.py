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

def test_statements_are_read_from_kb_facts_not_parsed_from_email():
    rows = [
        {"fact_type": "supplier_statement", "fact_id": "f-kind", "attribute": "kind", "value": "dispatch_info",
         "subject": {"statement_no": "ST-1", "material_id": "PART-A", "supplier_id": "SUP-A"},
         "as_of": "2030-01-11T17:00:00+00:00", "observed_at": None},
        {"fact_type": "supplier_statement", "fact_id": "f-val", "attribute": "value", "value": "Loaded, 200 units.",
         "subject": {"statement_no": "ST-1", "material_id": "PART-A", "supplier_id": "SUP-A"},
         "as_of": "2030-01-11T17:00:00+00:00", "observed_at": None},
        {"fact_type": "po_line", "fact_id": "x", "attribute": "qty", "value": 200, "subject": {"po": "P1"}},
    ]
    [st] = broker.statements_from_rows(rows)
    assert st.kind == "dispatch_info" and st.part_id == "PART-A" and st.supplier_id == "SUP-A"
    assert st.text == "Loaded, 200 units." and st.fact_id == "f-kind"
    assert st.at == "2030-01-11T17:00:00+00:00"
