"""The monitor end to end: the pack's cancellation mail, kb, the ledger, the hedger and the live screen.

The mail path, kb, the plan store and the record log are real. The model answers are replayed and the launch
facts are synthetic. Everything runs on a local throwaway Postgres and nothing can send.
"""

import importlib.util
import json
import os
import re
import shutil
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest
from psycopg import conninfo, sql
from psycopg.types.json import Json

from agents import broker, llm, monitor
from kb import consumer, db as kb_db, rules
from kb.config import KbSettings
from kb.ingest import FileReader
from kb.llm.base import LLMClient, Routing
from kb.loader import load_contract
from kb.mail_source import MailSource
from plan import clock, db as plan_db
from sourcing import promises, reliability
from sourcing.facts import Facts
from tracker import lookahead_decisions
from tracker.lookahead import Lookahead, append_records

from approvals_world import world as approval_world
from sourcing_world import DEADLINE, NOW, World

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "scenarios" / "artbook"
spec = importlib.util.spec_from_file_location("demo_reset", ROOT / "ops" / "demo" / "reset.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)

URL = os.environ.get("TEST_DATABASE_URL")
VALUES = {"unit_loss": "100", "budget": "300", "part": "BOOK", "offer_quantity": "600", "offer_price": "4", "offer_b_price": "5"}
GOAL = {"kind": "goal", "statement": "Every Deluxe copy on time.", "unit_loss_eur": 100}


class ReplayedExtraction(LLMClient):
    name, needs_images = "pack-replay", False

    def classify(self, document, kinds):
        return Routing("kind", "supplier_email", "replayed", self.label)

    def extract(self, document, structure, schema, contract):
        text = document.text
        said = datetime.fromisoformat(re.search(r"(?m)^Date: (.+)$", text)[1].strip()).isoformat()
        kind = "cancel" if "we cancel" in text else "confirm" if "load within" in text else None
        sentence = next((line.strip() for line in text.splitlines() if "within" in line or "cancel" in line), text.strip())
        return {"source": {}, "doc_kind": structure.name, "doc_kind_confidence": "high", "languages": ["en"],
                "spec_version": "1", "extracted_by": self.name, "doc_date": said, "unreadable": [],
                "proposed_fact_types": [],
                "facts": [{"fact_type": "supplier_statement", "subject": {"statement_no": "1"},
                           "attribute": attribute, "value": value, "as_of": said, "confidence": "high",
                           "locator": {"line": 1}, "evidence": "replayed answer"}
                          # like live extraction: the kind, and the sentence itself as `value`, which the broker
                          # grounds a promise's duration in
                          for attribute, value in (("promise_kind", kind), ("value", sentence))] if kind else []}


class KbWithRows:
    """The real kb, plus the synthetic approval and category rows."""

    def __init__(self, conn, rows):
        self.conn, self.rows = conn, rows

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def execute(self, query, *args):
        found = self.conn.execute(query, *args)
        if query != "SELECT * FROM current_facts":
            return found
        rows = [*found.fetchall(), *self.rows]
        return SimpleNamespace(fetchall=lambda: rows)


def launch_facts() -> World:
    w = World()
    w.add("plan_edition", {"product_id": "P-A"}, preorders=1100)
    w.add("plan_edition", {"product_id": "P-B"}, preorders=1100)
    w.drop("stock_batch", material_id="BOOK")
    w.stock("BOOK", 1400)
    w.add("component", {"material_id": "BOOK"}, category="Paper & Board")
    for material, suppliers in (("CARD", ("S1", "S2")), ("BOOK", ("S3",)), ("CASE", ("S1", "S2"))):
        for supplier in suppliers:
            w.add("supplier_part", {"material_id": material, "supplier_id": supplier}, primary="Y")
    for supplier in ("S1", "S2", "S3"):
        w.add("supplier", {"supplier_id": supplier}, status="active", qualification_status="qualified")
    return w


@pytest.fixture
def demo_run(tmp_path, monkeypatch):
    if not URL:
        pytest.skip("TEST_DATABASE_URL not set")
    if conninfo.conninfo_to_dict(URL).get("host") not in demo.LOOPBACK:
        pytest.skip("only a local throwaway Postgres")
    monkeypatch.delenv("PLAN_SCHEMA", raising=False)
    monkeypatch.delenv(clock.ENV_VAR, raising=False)
    name = f"monitor_{uuid.uuid4().hex[:8]}_demo"
    with psycopg.connect(URL, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            yield from started(conninfo.make_conninfo(URL, dbname=name), tmp_path)
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def started(url, tmp_path):
    """A rehearsal after the check-in: one waiting promise of the persona and a log with a goal."""
    pack, log, blobs = demo.load_pack(PACK), tmp_path / "log", tmp_path / "mail"
    structured, seed = tmp_path / "structured", tmp_path / "seed.yaml"
    shutil.copytree(ROOT / "structured" / "contract", structured / "contract")
    seed.write_text("rules: []\nsettings:\n  confidence_map: {high: 0.9, medium: 0.75, low: 0.4}\n")
    settings = KbSettings(database_url=url, schema="kb", data_dir=tmp_path / "data",
                          structured_dir=structured, seed_path=seed, llm_adapter=None)
    demo.reset(url, pack, DEADLINE.isoformat(), VALUES, log)
    source, extractor = MailSource("mail", blobs), ReplayedExtraction()

    with kb_db.connect(url, "kb") as kb, plan_db.connect(url) as plan:
        kb_db.migrate(kb, "kb")
        rules.seed(kb, seed)
        load_contract(kb, structured)
        kb.commit()

        def deliver(beat):
            assert demo.replay(url, pack, beat, log, blobs).new
            source.catch_up(kb, structured, {})
            consumer.extract(kb, settings, FileReader(settings.data_dir), extractor, source, {"high": 0.9})
            kb.commit()

        deliver("check_in")
        [said] = broker.read_statements(kb)
        kb.commit()
        persona = pack["persona"]["persona_id"]
        broker.save_promise(plan, promises.promise_from_statement(
            promise_id="p", supplier_id=persona, part_id="BOOK", what="loads", fallback="use the backup",
            made_at=said.at, within="within 4 h", sources=[said.source()]))
        clock.set_scenario(plan, NOW, rate=0, by="test")
        plan.commit()
        history = reliability.history([], [])
        card = reliability.card(history, persona, scenario_prior={"value": 0.7, "label": "test prior"})
        append_records(log, [GOAL], producer="app")
        append_records(log, [card, {"kind": "decision", "decision_id": "old", "status": "open", "lane": "BOOK",
                                    "title": "Rely on the persona"}], producer="agent:test")
        yield SimpleNamespace(url=url, log=log, kb=kb, plan=plan, deliver=deliver, persona=persona)


def offer(plan, offer_id, supplier, price):
    plan.execute("INSERT INTO offers (offer_id, supplier_id, part_id, quantity, unit_price_eur, lead_time_h, sources) "
                 "VALUES (%s, %s, 'BOOK', 600, %s, 4, %s)",
                 (offer_id, supplier, price, Json([{"scenario_input": "test offer"}])))
    plan.commit()


def screen(log):
    return Lookahead(log, clock=SimpleNamespace(read=lambda: (NOW, True)))


def test_the_cancellation_mail_becomes_a_hedged_decision_on_the_live_screen(demo_run):
    r, facts = demo_run, launch_facts()
    offer(r.plan, "FAILED", r.persona, 1)       # cheaper, but from the supplier that cancels
    offer(r.plan, "BACKUP", "S9", 4)
    rows = [*approval_world(), *facts.current]
    watch = monitor.Monitor(r.plan, KbWithRows(r.kb, rows), llm.ReplayClient(
        ROOT / "tests" / "fixtures" / "replay" / "hedger_debate.json"), r.log,
        load_facts=lambda *_: Facts(facts.current, facts.believed, facts.issues))
    la = screen(r.log)
    page = la.subscribe()
    assert watch.tick() == [], "the check-in alone breaks nothing"

    r.deliver("cancellation")
    [decision_id] = watch.tick()
    for record in la._read_new():
        la._broadcast(record)
    assert not page.empty(), "an open page is pushed the records"

    state = la.state()
    cards = {d["decision_id"]: d for d in state["decisions"]}
    assert cards["old"]["status"] == "superseded"
    assert [i for i, d in cards.items() if d["status"] == "open"] == [decision_id]
    card = cards[decision_id]
    assert re.fullmatch(r"[\w.-]+", decision_id), "the accept route takes no other id"
    assert "name" not in card["signer"], "today's route takes a name on the card as the one signature"
    assert "warehouse" in card["signer"]["role"].lower(), "the release is shown next to the value approval"
    assert card["producer"] == "monitor" and card["offer_id"] == "BACKUP" and card["actions"]
    assert card["category"] == "Paper & Board" and card["delivery_site"] == "HUB"
    assert card["hedge"]["loss_per_copy_eur"] == 100 and card["hedge"]["copies_at_risk"] == 300
    [promise] = state["promises"]
    assert promise["state"] == "broken"
    [supplier] = state["suppliers"]
    assert supplier["history"][-1]["value"] < supplier["history"][0]["value"]

    saved = r.plan.execute("SELECT recommendation_id::text AS id, kind FROM recommendations").fetchone()
    assert saved["id"] == card["recommendation_id"] and saved["kind"] == "hedge"
    diff = r.plan.execute("SELECT payload FROM timeline_events").fetchone()["payload"]["diff"]
    assert diff["before"] == ["old"] and diff["after"]["decision_id"] == decision_id

    shown = lookahead_decisions.card(la, decision_id, SimpleNamespace(facts=lambda: rows))
    assert "warehouse" in shown["signer"]["role"].lower()
    assert shown["approval"]["status"] == "needs_human", "the site release is not lost on the way to the card"

    size = r.log.stat().st_size
    assert watch.tick() == [] and r.log.stat().st_size == size, "a handled break is not published again"


def test_without_facts_or_offers_the_card_says_so_and_cannot_be_signed(demo_run):
    r = demo_run
    watch = monitor.Monitor(r.plan, r.kb, llm.ReplayClient([]), r.log)
    r.deliver("cancellation")
    [decision_id] = watch.tick()
    card = screen(r.log).decision(decision_id)
    assert card["status"] == "open" and card["actions"] == []
    assert "No backup can be proposed" in card["recommendation"] and "use the backup" in card["recommendation"]


def test_a_failed_append_is_repeated_under_the_same_decision_id(demo_run, monkeypatch):
    r = demo_run
    watch = monitor.Monitor(r.plan, r.kb, llm.ReplayClient([]), r.log)
    r.deliver("cancellation")
    with monkeypatch.context() as broken:
        broken.setattr(monitor, "append_records", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))
        with pytest.raises(OSError):
            watch.tick()
    [decision_id] = watch.tick()
    assert screen(r.log).decision(decision_id)["status"] == "open"


def test_run_is_single_and_reconnects_and_publishes_without_a_restart(demo_run, monkeypatch):
    r, stop = demo_run, threading.Event()
    monkeypatch.setattr(monitor, "BACKOFF", (0.1,))
    threads = [threading.Thread(target=monitor.run, args=(r.url, llm.ReplayClient([]), r.log, stop),
                                kwargs={"idle": 0.1}, daemon=True) for _ in range(2)]

    def holders():
        with psycopg.connect(r.url, autocommit=True) as conn:
            return [row[0] for row in conn.execute(
                "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND granted AND objid = %s "
                "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())", (monitor.LOCK,))]

    def until(check):
        end = time.monotonic() + 15
        while not check():
            assert time.monotonic() < end, "timed out"
            time.sleep(0.05)

    for thread in threads:
        thread.start()
    try:
        until(lambda: len(holders()) == 1)
        [first] = holders()
        with psycopg.connect(r.url, autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(%s)", (first,))
        until(lambda: holders() and holders() != [first])
        r.deliver("cancellation")
        published = lambda: [d for d in screen(r.log).state()["decisions"] if d["decision_id"] != "old"]
        until(published)
        time.sleep(0.3)
        assert len(published()) == 1 and len(holders()) == 1
    finally:
        stop.set()
        for thread in threads:
            thread.join(5)
    assert not any(thread.is_alive() for thread in threads)


def test_site_and_category_are_never_guessed():
    assert monitor.site_of([]) == monitor.NO_SITE
    two = [{"fact_type": "packout", "attribute": "site_id", "value": site} for site in ("A", "B")]
    assert monitor.site_of(two) == monitor.NO_SITE
    assert monitor.category_of([], "BOOK") is None


@pytest.mark.parametrize("status, others, named", [
    ("resolved", {}, True),
    ("resolved", {"also_required": [{"role": "Site", "name": "B", "rule": "R2"}]}, False),
    ("resolved", {"signers": [{"role": "Lead", "name": "A"}, {"role": "Chief", "name": "C"}]}, False),
    ("needs_human", {}, False),
    ("held", {}, False),
])
def test_a_name_is_on_the_card_only_when_one_person_signs(status, others, named):
    approval = {"status": status, "signer": {"role": "Lead", "name": "A", "rule": "R1"},
                "signers": [{"role": "Lead", "name": "A"}], "also_required": [], "reasons": [], **others}
    shown = monitor.card_signer(approval)
    assert ("name" in shown) == named and "Lead" in shown["role"]
    for extra in approval["also_required"]:
        assert extra["role"] in shown["role"] and extra["rule"] in shown["rule"]


def test_the_monitor_brokers_the_check_in_so_the_cancellation_breaks_it(demo_run):
    r, facts = demo_run, launch_facts()
    r.plan.execute("DELETE FROM promises")            # nothing registered by hand: the monitor must broker it
    r.plan.commit()
    [said] = broker.read_statements(r.kb)
    r.kb.commit()
    register = {"stop_reason": "tool_use", "tool_calls": [{"name": "register_promise", "input": {
        "promise_id": "p", "supplier_id": r.persona, "what": "loads", "within": "within 4 h",
        "fallback": "use the backup", "part_id": "BOOK", "sources": [{"fact_id": said.fact_id}]}}]}
    script = [register, {"stop_reason": "end_turn", "text": '{"promises": ["p"], "drafts": [], "note": "watching"}'},
              *json.loads((ROOT / "tests" / "fixtures" / "replay" / "hedger_debate.json").read_text())]
    offer(r.plan, "BACKUP", "S9", 4)
    watch = monitor.Monitor(r.plan, KbWithRows(r.kb, [*approval_world(), *facts.current]), llm.ReplayClient(script),
                            r.log, load_facts=lambda *_: Facts(facts.current, facts.believed, facts.issues))
    assert watch.tick() == [], "the check-in alone breaks nothing"
    assert r.plan.execute("SELECT state FROM promises").fetchone()["state"] == "waiting"
    r.deliver("cancellation")
    [decision_id] = watch.tick()
    assert r.plan.execute("SELECT state FROM promises").fetchone()["state"] == "broken"
    assert decision_id in {d["decision_id"] for d in screen(r.log).state()["decisions"]}
    assert watch.tick() == [], "a statement is brokered once and a break published once"


def _offer_rows(**attrs):
    subject = {"supplier_id": "S9", "offer_ref": "Q-1", "item": "BOOK"}
    return [{"fact_id": f"f{i}", "fact_type": "offer", "subject": subject, "attribute": k, "value": v}
            for i, (k, v) in enumerate(attrs.items())]


def test_an_offer_read_from_mail_reaches_plan_offers_once(demo_run):
    from agents import kb_offers
    r = demo_run
    r.plan.execute("DELETE FROM offers")
    r.plan.commit()
    rows = _offer_rows(qty="600", unit_price="4,20 EUR", lead_time="within 48 h", valid_until="2026-12-01")
    assert kb_offers.sync(r.kb, r.plan, rows) == ["kb:S9/Q-1/BOOK"]
    row = r.plan.execute("SELECT * FROM offers").fetchone()
    assert (row["supplier_id"], row["part_id"], float(row["quantity"]), float(row["unit_price_eur"]),
            row["lead_time_h"]) == ("S9", "BOOK", 600.0, 4.2, 48.0)
    assert {s["fact_id"] for s in row["sources"]} == {"f0", "f1", "f2", "f3"}, "every value cites its fact"
    assert kb_offers.sync(r.kb, r.plan, rows) == [], "an offer is copied once"


def test_an_offer_value_that_cannot_be_read_stays_empty():
    from agents import kb_offers
    [o] = kb_offers.offers_from_rows(_offer_rows(qty="about 600", lead_time="2 working days", valid_until="soon"))
    assert o["quantity"] is None and o["lead_time_h"] is None and o["valid_until"] is None


def test_a_broker_failure_is_retried_and_does_not_stop_the_ledger(demo_run, monkeypatch):
    r, facts = demo_run, launch_facts()
    r.plan.execute("DELETE FROM promises")
    r.plan.commit()
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise RuntimeError("model down")

    monkeypatch.setattr(monitor.broker, "run_broker", boom)
    watch = monitor.Monitor(r.plan, KbWithRows(r.kb, [*approval_world(), *facts.current]), llm.ReplayClient([]),
                            r.log, load_facts=lambda *_: Facts(facts.current, facts.believed, facts.issues))
    assert watch.tick() == [] and watch.tick() == []
    assert len(calls) == 2, "a statement the broker could not handle is tried again, not marked done"
    assert not r.plan.execute("SELECT 1 FROM timeline_events WHERE type = %s", (monitor.BROKERED,)).fetchall()


def test_a_promise_whose_sources_are_bare_fact_ids_still_re_plans(demo_run):
    # Rehearsal: the broker stores sources as bare fact ids as well as {"fact_id": ...}; the mix made the re-plan
    # fail with "an input needs a source" and no card appeared.
    r, facts = demo_run, launch_facts()
    offer(r.plan, "BACKUP", "S9", 4)
    [fact] = r.plan.execute("SELECT sources FROM promises WHERE promise_id = 'p'").fetchone()["sources"]
    r.plan.execute("UPDATE promises SET sources = %s WHERE promise_id = 'p'", (Json([fact["fact_id"]]),))
    r.plan.commit()
    rows = [*approval_world(), *facts.current]
    watch = monitor.Monitor(r.plan, KbWithRows(r.kb, rows), llm.ReplayClient(
        ROOT / "tests" / "fixtures" / "replay" / "hedger_debate.json"), r.log,
        load_facts=lambda *_: Facts(facts.current, facts.believed, facts.issues))
    r.deliver("cancellation")
    [decision_id] = watch.tick()
    card = {d["decision_id"]: d for d in screen(r.log).state()["decisions"]}[decision_id]
    assert card["offer_id"] == "BACKUP" and card["actions"]


def test_a_timeout_is_not_marked_done_and_is_asked_a_limited_number_of_times(demo_run):
    r, asked = demo_run, []
    r.plan.execute("DELETE FROM promises")
    r.plan.commit()

    class Down(llm.LLMClient):
        def message(self, **request):
            asked.append(request)
            raise llm.AgentStop("timeout")

    watch = monitor.Monitor(r.plan, r.kb, Down(), r.log)
    for _ in range(monitor.BROKER_ATTEMPTS + 2):
        watch.tick()
    assert len(asked) == monitor.BROKER_ATTEMPTS
    assert [tool["name"] for tool in asked[0]["tools"]] == ["register_promise"]
    assert not r.plan.execute("SELECT 1 FROM timeline_events WHERE type = %s", (monitor.BROKERED,)).fetchall()


@pytest.mark.parametrize("attrs, field, value", [
    ({"unit_price": "1.200 EUR"}, "unit_price_eur", None),
    ({"qty": "10,000"}, "quantity", None),
    ({"unit_price": "420", "price_basis": "per 100"}, "unit_price_eur", None),
    ({"unit_price": "4,20 EUR", "price_basis": "per piece"}, "unit_price_eur", 4.2),
])
def test_an_ambiguous_offer_number_is_never_guessed(attrs, field, value):
    from agents import kb_offers
    assert kb_offers.offers_from_rows(_offer_rows(**attrs))[0][field] == value


def test_one_impossible_date_does_not_lose_the_other_offers(demo_run):
    from agents import kb_offers
    r, good = demo_run, _offer_rows(qty="600")
    bad = [{**row, "subject": {**row["subject"], "offer_ref": "Q-2"}} for row in _offer_rows(valid_until="2026-02-30")]
    assert kb_offers.sync(r.kb, r.plan, bad + good) == ["kb:S9/Q-1/BOOK"]


def test_run_gives_the_broker_no_connection_of_its_own():
    assert not hasattr(monitor, "_mail_tools"), "mail tools would read another database url than the monitor's"
