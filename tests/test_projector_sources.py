"""The projector's records name the source of their numbers. Synthetic world, real builder and models."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from projector import artbook
from sourcing import hedge, reliability
from sourcing.facts import Facts
from tracker.lookahead_records import validate


def fact(fact_id, fact_type, subject, attribute, value):
    return {"fact_id": fact_id, "fact_type": fact_type, "subject": subject, "attribute": attribute, "value": value,
            "as_of": None, "path": "synthetic", "sha256": "synthetic", "confidence": 0.9}


def records(bom=True):
    rows = []
    for i in range(12):
        line = {"po_number": f"order-{i}", "po_line": 1}
        for key, value in {"supplier_id": "source", "promised_date": "2030-01-10", "qty": 5, "material_id": "part",
                           "actual_delivery_date": "2030-01-10" if i < 6 else "2030-01-14"}.items():
            rows.append(fact(f"history-{i}-{key}", "po_line", line, key, value))
    if bom:
        rows.append(fact("bom-fact", "bom_line", {"parent_id": "product", "child_id": "part"}, "qty_per", 1))
    history = reliability.history(rows, [])
    end = datetime(2030, 1, 2, 18, tzinfo=timezone.utc)
    figure = lambda value, fact_id: {"value": value, "fact_ids": [fact_id]}
    edition = {"name": "Edition", "product_id": "product", "demand": figure(90, "demand-fact"),
               "open": figure(80, "open-fact"), "covered": figure(50, "covered-fact")}
    spot = {"part_id": "part", "exposure_eur": 300, "reason": "short", "rank": 1,
            "detail": {"copies_at_risk": figure(30, "risk-fact"), "status": "critical"},
            "sources": [{"goal_line": "ship it"}, {"fact_id": "risk-fact"}]}
    world = SimpleNamespace(
        facts=Facts(rows), history=history, windows=[], deadline=end, profiles={},
        coverage=lambda now: {}, product=lambda name, cov: edition, spots=lambda cov, goal: [spot],
        suppliers=lambda: {}, site=lambda: {"site_id": "site"}, part_name=lambda part: "Part",
        parts_of=lambda supplier: [], card=lambda sid, promises=(): reliability.card(history, sid, promises),
        price=lambda sid, part, on: hedge.Input(2, {"fact_id": "price-fact"}),
        signer=lambda *a: {"role": "approver", "status": "needs_human", "reasons": ["unclear"]})
    at = lambda hour: f"2030-01-01T{hour:02d}:00:00+00:00"
    scenario = {
        "start": at(7), "edition": "Edition", "decision_at": at(15),
        "goal": {"statement": "ship it", "unit_loss_eur": 10, "sacrifice": "none", "budget_eur": 50},
        "offer": {"supplier_id": "outside", "history_from": "source", "label": "Outside", "city": "", "country": "",
                  "lang": "en", "units": 30, "unit_price_eur": 1, "received_at": at(8), "loads_at": at(12)},
        "backup": {"supplier_id": "source", "units": 30, "deposit": "6 EUR", "ordered_at": at(9), "load_due": at(11),
                   "reply_at": at(10), "loaded_at": at(11), "arrives_at": at(17)},
        "cancellation": {"at": at(13), "reason": "no truck"}}
    return artbook.build(world, scenario)


@pytest.fixture(scope="module")
def built():
    return records()


def test_every_number_record_names_a_source(built):
    for rec in built:
        validate(rec)
        kind = rec["kind"]
        if kind == "supplier":
            assert rec["score"]["sources"], rec["supplier_id"]
        elif kind == "plan":
            assert rec["coverage"]["sources"]
            assert all(lane["sources"] for lane in rec["lanes"])
        elif kind == "decision":
            assert rec["cost"]["sources"] and rec["impact"]["sources"]
        elif kind == "promise" and "what" in rec:
            assert rec["sources"]
        elif kind == "event":
            assert rec["detail"]["sources"], rec["title"]
            assert all(row["sources"] for row in rec["detail"].get("reasoning", [])), rec["title"]
            if rec.get("impact"):
                assert rec["impact"]["sources"]


def test_numbers_keep_the_facts_they_were_computed_from(built):
    cost = next(r for r in built if r["kind"] == "decision")["cost"]
    assert {"fact_id": "price-fact"} in cost["sources"]
    start = next(r for r in built if r["kind"] == "plan")
    assert start["coverage"]["sources"] == [{"fact_id": "covered-fact"}, {"fact_id": "open-fact"}]


def per_copy_sources(built):
    rows = [row for r in built if r["kind"] == "event" for row in r["detail"].get("reasoning", [])]
    return next(row["sources"] for row in rows if row["text"] == "Units per copy")


def test_a_bom_quantity_is_a_fact_not_a_scenario_input(built):
    assert per_copy_sources(built) == [{"fact_id": "bom-fact"}]


def test_a_missing_bom_quantity_says_it_is_assumed():
    assert "assumed" in per_copy_sources(records(bom=False))[0]["scenario_input"]
