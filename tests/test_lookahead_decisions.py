"""Decision card, accept and reject on a throwaway Postgres. Mail runs in dry_run. Everything is invented."""

import dataclasses
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from psycopg import sql

from kb import db as kb_db
from mail import db as mail_db
from mail import drafts
from mail.config import load_settings
from plan import contracts
from plan import db as plan_db
from sourcing import reliability
from tracker import lookahead_decisions as decisions
from tracker.lookahead import Lookahead, LookaheadError

from approvals_world import world

URL = os.environ.get("TEST_DATABASE_URL")
FIXTURE = Path(__file__).parent / "fixtures" / "lookahead_rehearsal.jsonl"
NOW = "2030-03-11T06:00:00+00:00"
TO = "desk@supplier.example.net"
SIGNER = "Jo James"


class Clock:
    def __init__(self, now=NOW):
        self.at = datetime.fromisoformat(now)

    def read(self):
        return self.at, True


class NoSmtp:
    def send(self, msg):
        raise AssertionError("SMTP must not be reached")


def payment(amount=1000, whole=None):
    return {"action": "purchase_order", "amount_eur": amount, "order_value_eur": whole or amount,
            "amount_source": {"scenario_input": "test amount"}}


def decision(**more):
    return {"kind": "decision", "decision_id": "D", "status": "open", "title": "Confirm the backup",
            "category": "Paper & Board", "actions": [payment()],
            "signer": {"role": "Whoever the producer says", "name": "Mallory Producer"}, **more}


def screen(tmp_path, *records, now=NOW):
    la = Lookahead(tmp_path / "log.jsonl", clock=Clock(now))
    la.append(list(records), "agent")
    return la


def refused(status, step, *args):
    with pytest.raises(LookaheadError) as exc:
        step(*args)
    assert exc.value.status == status
    return str(exc.value)


@pytest.fixture
def stores(tmp_path, monkeypatch):
    if not URL:
        pytest.skip("TEST_DATABASE_URL not set")
    names = {part: f"{part}_u2_{uuid.uuid4().hex[:8]}" for part in ("plan", "mail")}
    monkeypatch.setenv("PLAN_SCHEMA", names["plan"])
    settings = dataclasses.replace(
        load_settings(load_files=False), database_url=URL, schema=names["mail"], address="buyer@example.com",
        send_mode="dry_run", data_dir=tmp_path / "mail_data")
    plan = plan_db.connect(URL, names["plan"], autocommit=True)
    plan_db.migrate(plan, names["plan"])
    mail = mail_db.connect(URL, names["mail"], autocommit=True)
    mail_db.migrate(mail, names["mail"])
    mail.execute("INSERT INTO contacts (address, supplier_id, is_test, send_allowed, source) "
                 "VALUES (%s, 'S-B', false, true, 'test')", (TO,))
    out = decisions.Stores(settings, NoSmtp())
    out.facts = world
    out.db = {"plan": plan, "mail": mail}
    yield out
    for part, conn in out.db.items():
        conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(names[part])))
        conn.close()


def draft(stores, subject="Backup order"):
    made = drafts.create_draft(stores.db["mail"], stores.settings, subject=subject, lang="en", to=[TO],
                               supplier_id="S-B", body_local="Please confirm.", body_en="Please confirm.",
                               cause={"action_id": "test"}, created_by="agent:test")
    return str(made["draft_id"])


def row(stores):
    return stores.db["plan"].execute("SELECT * FROM decisions WHERE decision_id = 'D'").fetchone()


def count(stores, table):
    query = sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier(table))
    return stores.db["mail"].execute(query).fetchone()["n"]


# ---------- signer ----------

@pytest.mark.parametrize("amount, whole, role, name", [
    (1000, 1000, "Area Manager Paper & Board", "Jo James"),
    (600, 8000, "Purchasing Director", "Dr. Ben Baker"),
    (9000, 8000, "Purchasing Director", "Dr. Ben Baker"),
])
def test_signer_comes_from_routing_and_a_split_cannot_lower_it(amount, whole, role, name):
    out = decisions.routed_signer(decision(actions=[payment(amount, whole)]), world(), datetime.fromisoformat(NOW))
    assert out["status"] == "resolved"
    assert (out["signer"]["role"], out["signer"]["name"]) == (role, name)
    assert "AP-1 v2.0 section 2" in out["signer"]["rule"] and out["signer"]["reason"] and out["sources"]


@pytest.mark.parametrize("change", [
    {"actions": []},
    {"actions": [payment(700000)]},
    {"actions": [payment(1000), payment(9000)]},
    {"actions": [{**payment(), "order_value_eur": None}]},
    {"actions": [{**payment(), "action": "unheard_of"}]},
])
def test_no_single_signer_means_no_name_and_a_reason(change):
    out = decisions.routed_signer(decision(**change), world(), datetime.fromisoformat(NOW))
    assert out["status"] == "needs_human" and "name" not in out["signer"] and out["signer"]["reason"]


def test_no_policy_facts_means_no_signer():
    out = decisions.routed_signer(decision(), [], datetime.now(timezone.utc))
    assert out["status"] == "needs_human" and "name" not in out["signer"]


def test_signer_day_is_the_launch_days_own():
    la = type("Screen", (), {"_latest": lambda self, kind: {"deadline": "2030-03-12T20:00:00+08:00"}})()
    assert decisions._team_time(la, "2030-03-11T17:00:00+00:00").date().day == 12


def test_card_shows_the_routed_signer_not_the_producers(tmp_path, stores):
    shown = decisions.card(screen(tmp_path, decision()), "D", stores)
    assert shown["signer"]["name"] == SIGNER and shown["signer"]["role"] == "Area Manager Paper & Board"
    assert "AP-1" in shown["signer"]["rule"] and shown["signer"]["reason"]
    contracts.validate("decision", {k: v for k, v in shown.items() if k not in ("kind", "id", "producer")})


def test_facts_are_read_from_kb(stores):
    schema = f"kb_u2_{uuid.uuid4().hex[:8]}"
    conn = kb_db.connect(URL, schema, autocommit=True)
    try:
        kb_db.migrate(conn, schema)
        real = decisions.Stores(dataclasses.replace(stores.settings, kb_schema=schema))
        assert real.facts() == []
    finally:
        conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
        conn.close()


# ---------- accept ----------

@pytest.mark.parametrize("who", ["", " ", "agent:hedger", "Mail:worker", "system"])
def test_accept_needs_a_named_person(tmp_path, who):
    refused(400, decisions.accept, screen(tmp_path, decision()), "D", {"approver": who})


def test_only_the_routed_signer_accepts(tmp_path, stores):
    la = screen(tmp_path, decision(draft_ids=[draft(stores)]))
    assert SIGNER in refused(403, decisions.accept, la, "D", {"approver": "Mallory Producer"}, stores)
    assert la.decision("D")["status"] == "open" and row(stores) is None and count(stores, "approvals") == 0


def test_accept_without_a_signer_is_refused(tmp_path, stores):
    la = screen(tmp_path, decision(actions=[]))
    refused(409, decisions.accept, la, "D", {"approver": SIGNER}, stores)
    assert row(stores) is None


def test_accept_writes_the_plan_and_sends_exactly_the_attached_drafts(tmp_path, stores):
    attached, other = [draft(stores, "Order"), draft(stores, "Remainder")], draft(stores, "Not attached")
    la = screen(tmp_path, decision(draft_ids=attached))
    assert decisions.accept(la, "D", {"approver": SIGNER}, stores)["sent"] == attached
    saved = row(stores)
    assert (saved["status"], saved["decided_by"]) == ("accepted", SIGNER)
    assert saved["signer"]["name"] == SIGNER and saved["decided_at"] == datetime.fromisoformat(NOW)
    sends = stores.db["mail"].execute("SELECT draft_id::text AS d, state FROM sends").fetchall()
    assert sorted(s["d"] for s in sends) == sorted(attached) and {s["state"] for s in sends} == {"dry_run"}
    signed = stores.db["mail"].execute("SELECT * FROM approvals").fetchall()
    assert {(a["approver"], a["approver_role"]) for a in signed} == {(SIGNER, "Area Manager Paper & Board")}
    assert all("AP-1" in a["policy_ref"] for a in signed)
    assert drafts.load_draft(stores.db["mail"], other)["status"] == "draft"
    record = la.decision("D")
    assert record["status"] == "accepted" and record["signer"]["name"] == SIGNER
    contracts.validate_record(la.records[-1])
    refused(409, decisions.accept, la, "D", {"approver": SIGNER}, stores)
    assert count(stores, "sends") == 2


def test_an_unknown_draft_stops_before_anything_is_sent(tmp_path, stores):
    for bad in (str(uuid.uuid4()), "not-an-id"):
        la = screen(tmp_path, decision(draft_ids=[draft(stores), bad]))
        refused(409, decisions.accept, la, "D", {"approver": SIGNER}, stores)
        assert row(stores) is None and count(stores, "approvals") == count(stores, "sends") == 0


def test_a_retry_after_a_failed_send_does_not_send_twice(tmp_path, stores):
    first, second = draft(stores, "First"), draft(stores, "Second")
    mail = stores.db["mail"]
    kept = drafts.load_draft(mail, second)["subject"]
    mail.execute("UPDATE drafts SET subject = 'changed' WHERE draft_id = %s", (second,))
    la = screen(tmp_path, decision(draft_ids=[first, second]))
    refused(502, decisions.accept, la, "D", {"approver": SIGNER}, stores)
    assert la.decision("D")["status"] == "open" and row(stores) is None and count(stores, "sends") == 1
    mail.execute("UPDATE drafts SET subject = %s WHERE draft_id = %s", (kept, second))
    assert decisions.accept(la, "D", {"approver": SIGNER}, stores)["sent"] == [second]
    assert count(stores, "sends") == 2 and row(stores)["status"] == "accepted"


def test_a_lost_record_is_rewritten_without_sending_again(tmp_path, stores):
    card = decision(draft_ids=[draft(stores)])
    decisions.accept(screen(tmp_path / "a", card), "D", {"approver": SIGNER}, stores)
    la = screen(tmp_path / "b", card)
    assert decisions.accept(la, "D", {"approver": SIGNER}, stores)["sent"] == []
    assert la.decision("D")["status"] == "accepted" and count(stores, "sends") == 1


def test_rehearsal_accept_reaches_no_database_and_no_mail(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a rehearsal must not open stores")
    monkeypatch.setattr(decisions, "Stores", refuse)
    la = Lookahead(tmp_path / "log.jsonl", FIXTURE)
    while not la.state()["decisions"]:
        la.advance(force=True)
    key = la.state()["decisions"][0]["decision_id"]
    assert decisions.accept(la, key, {"approver": "Test Person"})["sent"] == []
    assert la.decision(key)["status"] == "accepted"


# ---------- reject ----------

def test_reject_records_the_reason_and_sends_nothing(tmp_path, stores):
    waiting = draft(stores)
    la = screen(tmp_path, decision(draft_ids=[waiting]))
    refused(400, decisions.reject, la, "D", {"approver": SIGNER}, stores)
    decisions.reject(la, "D", {"approver": SIGNER, "reason": "Too expensive"}, stores)
    saved = row(stores)
    assert (saved["status"], saved["note"], saved["decided_by"]) == ("rejected", "Too expensive", SIGNER)
    assert la.decision("D")["reason"] == "Too expensive"
    contracts.validate_record(la.records[-1])
    assert count(stores, "approvals") == count(stores, "sends") == 0
    refused(409, decisions.accept, la, "D", {"approver": SIGNER}, stores)


# ---------- score and end state ----------

def test_card_shows_the_score_dropping_after_the_cancellation(tmp_path, stores):
    start = reliability.card(reliability.history([], []), "S-A",
                             scenario_prior={"value": 0.8, "k": 5, "label": "test prior"})
    after = reliability.update(start, "broken", "P-1")
    la = screen(tmp_path, start, {"kind": "event", "type": "cancellation", "title": "Cancelled", "lane": "L",
                                  "supplier_id": "S-A"}, after, decision(lane="L"))
    drop, = decisions.card(la, "D", stores)["score_drops"]
    assert (drop["before"], drop["after"]) == (start["score"]["value"], after["score"]["value"])
    assert drop["after"] < drop["before"] and "P-1" in drop["cause"] and drop["sources"]
    assert decisions.score_drops(la.state(), decision(lane="another lane")) == []


def test_goal_turns_green_after_accept_only_when_coverage_meets_it(tmp_path, stores):
    def plan(covered):
        return {"kind": "plan", "coverage": {"covered": covered, "needed": 4200, "unit": "units"}}

    la = screen(tmp_path, {"kind": "goal", "edition": "Deluxe"}, plan(3100), decision())
    out = decisions.accept(la, "D", {"approver": SIGNER}, stores)["end_state"]
    assert not out["met"] and out["text"] == "Deluxe 3,100 of 4,200 covered"
    la.append([plan(4200)], "monitor")
    out = decisions.card(la, "D", stores)["end_state"]
    assert out["met"] and out["text"] == "Deluxe 4,200 of 4,200 covered"
