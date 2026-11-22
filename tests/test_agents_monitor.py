"""The monitor end to end: the pack's cancellation mail, kb, the ledger, the hedger and the live screen.

The mail path, kb, the plan store and the record log are real. The model answers are replayed and the launch
facts are synthetic. Everything runs on a local throwaway Postgres and nothing can send.
"""

import importlib.util
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
VALUES = {"unit_loss": "100", "budget": "300", "part": "BOOK", "offer_quantity": "600", "offer_price": "4"}
GOAL = {"kind": "goal", "statement": "Every Deluxe copy on time.", "unit_loss_eur": 100}


class ReplayedExtraction(LLMClient):
    name, needs_images = "pack-replay", False

    def classify(self, document, kinds):
        return Routing("kind", "supplier_email", "replayed", self.label)

    def extract(self, document, structure, schema, contract):
        text = document.text
        said = datetime.fromisoformat(re.search(r"(?m)^Date: (.+)$", text)[1].strip()).isoformat()
        kind = "cancel" if "we cancel" in text else "confirm" if "load within" in text else None
        return {"source": {}, "doc_kind": structure.name, "doc_kind_confidence": "high", "languages": ["en"],
                "spec_version": "1", "extracted_by": self.name, "doc_date": said, "unreadable": [],
                "proposed_fact_types": [],
                "facts": [{"fact_type": "supplier_statement", "subject": {"statement_no": "1"},
                           "attribute": "promise_kind", "value": kind, "as_of": said, "confidence": "high",
                           "locator": {"line": 1}, "evidence": "replayed answer"}] if kind else []}


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
