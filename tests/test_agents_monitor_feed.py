"""The monitor shows each step of the demo on the Lookahead screen: an offer read from mail, a watched check-in, and
(by the re-plan) the break. Uses the D1 art-book pack through the monitor tests' rehearsal fixture."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from psycopg.types.json import Json

from agents import llm, monitor, monitor_feed
from sourcing.facts import Facts
from test_agents_monitor import ROOT, KbWithRows, approval_world, demo_run, launch_facts, offer, screen  # noqa: F401


def _monitor_lines(log):
    return [r for r in map(json.loads, log.read_text().splitlines()) if r.get("producer") == monitor.PRODUCER]


def _watch(r, facts):
    script = json.loads((ROOT / "tests" / "fixtures" / "replay" / "hedger_debate.json").read_text())
    return monitor.Monitor(r.plan, KbWithRows(r.kb, [*approval_world(), *facts.current]), llm.ReplayClient(script),
                           r.log, load_facts=lambda *_: Facts(facts.current, facts.believed, facts.issues))


def mail_offer(plan, supplier):
    plan.execute("INSERT INTO offers (offer_id, supplier_id, part_id, quantity, unit_price_eur, lead_time_h, sources) "
                 "VALUES (%s, %s, 'BOOK', 600, 4.2, 8, %s)",
                 (f"kb:{supplier}/Q-1/BOOK", supplier, Json([{"fact_id": "offer-fact"}])))
    plan.commit()


def test_each_step_shows_once_on_the_screen(demo_run):
    r, facts = demo_run, launch_facts()
    mail_offer(r.plan, "S9")
    offer(r.plan, "BACKUP", "S9", 4)          # a scenario input offer is not news from mail: not shown as received
    watch = _watch(r, facts)

    assert watch.tick() == []
    shown = _monitor_lines(r.log)
    assert [(x["kind"], x.get("type")) for x in shown] == [
        ("event", monitor_feed.OFFER_RECEIVED), ("promise", None), ("event", monitor_feed.PROMISE_WATCHED)]
    received, watched = shown[0], shown[2]
    assert received["source"] == {"fact_id": "offer-fact"} and "S9" in received["title"]
    assert shown[1]["state"] == "waiting" and watched["promise_id"] == "p"
    state = screen(r.log).state()
    assert {x["promise_id"]: x for x in state["promises"]}["p"]["state"] == "waiting"
    assert {monitor_feed.OFFER_RECEIVED, monitor_feed.PROMISE_WATCHED} <= {e["type"] for e in state["events"]}

    assert watch.tick() == [] and len(_monitor_lines(r.log)) == 3, "nothing is shown twice"

    r.deliver("cancellation")
    [decision_id] = watch.tick()
    after = _monitor_lines(r.log)
    assert not [x for x in after[3:] if x.get("type") in (monitor_feed.OFFER_RECEIVED, monitor_feed.PROMISE_WATCHED)]
    state = screen(r.log).state()
    assert {x["promise_id"]: x for x in state["promises"]}["p"]["state"] == "broken"
    assert decision_id in {d["decision_id"] for d in state["decisions"]}


def test_a_restart_does_not_show_again(demo_run):
    r, facts = demo_run, launch_facts()
    mail_offer(r.plan, "S9")
    _watch(r, facts).tick()
    _watch(r, facts).tick()                    # a new monitor process, same database and log
    assert len(_monitor_lines(r.log)) == 3


def test_a_kept_promise_says_so_and_names_come_from_the_log():
    due = datetime(2026, 11, 22, 2, tzinfo=timezone.utc)
    promise = {"promise_id": "p", "supplier_id": "sup-1", "part_id": "BOOK", "what": "loads", "due": due,
               "proven_at": due, "sources": [{"fact_id": "f"}]}
    record, event = monitor_feed.promise_records(promise, "proven", "Named Supplier", due)
    assert record["state"] == "proven" and record["proven_at"] == due.isoformat()
    assert event["type"] == monitor_feed.PROMISE_KEPT and event["title"].startswith("Named Supplier kept")
    [received] = monitor_feed.offer_records({"offer_id": "kb:x", "supplier_id": "sup-1", "part_id": "BOOK",
                                             "quantity": None, "unit_price_eur": None, "sources": []}, "sup-1", due)
    assert received["title"] == "Offer received from sup-1: BOOK" and received["source"] is None
