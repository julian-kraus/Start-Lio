"""Scenarios A, B and C on the real facts, local only (skips without the data mirror).

The fixture holds the expected copies, the timing assumed for the two actions and a fingerprint of the facts
behind the figures (id and attribute, no values). The two action quantities are read from the hand analysis."""

import json
import re
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pytest

from plan import contracts
from sourcing import coverage, coverage_windows, simulate
from sourcing.facts import day, number
from test_sourcing_golden import ANALYSIS, MIRROR, real_facts

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "simulate_scenarios.json").read_text())
pytestmark = pytest.mark.skipif(not MIRROR.is_dir() or not ANALYSIS.is_file(), reason="no local data mirror")


def moved(facts, cited) -> str:
    was = FIXTURE["facts"]
    now = {i: f"{facts.by_id[i]['fact_type']}.{facts.by_id[i]['attribute']}" for i in cited}
    lines = [f"  gone  {i} {name}" for i, name in was.items() if i not in facts.by_id]
    lines += [f"  new   {i} {name}" for i, name in sorted(now.items()) if i not in was]
    lines += [f"  no longer used {i} {name}" for i, name in was.items() if i in facts.by_id and i not in now]
    return "facts that moved:\n" + ("\n".join(lines) or "  none, so the model moved")


def difference(facts, base, result, expected) -> str:
    """Empty when the result is the expected one, else the delta and the facts that moved."""
    by_id = {e["product_id"]: e["covered"]["value"] + e["packed"]["value"] for e in result["editions"]}
    got = [by_id[p] for p in result["priority"]["order"]]
    if got == expected:
        return ""
    delta = [g - e for g, e in zip(got, expected)]
    cited = [s["fact_id"] for s in base["sources"]]     # the fingerprint is of the facts with no action
    return f"got {got}, expected {expected}, delta {delta} (policy order)\n{moved(facts, cited)}"


@pytest.fixture(scope="module")
def world():
    facts = real_facts()
    launch = max(d for r in facts.rows("product") if (d := day(r.get("launch_date"))))
    end = datetime.combine(launch, time(23, 59), timezone(timedelta(hours=1)))
    windows = coverage_windows.pack_windows(facts, end, end)
    now, deadline = windows[0].start, windows[-1].end
    base = coverage.compute(facts, now=now, deadline=deadline)
    text = ANALYSIS.read_text()

    def quantity(pattern):
        return number(re.search(pattern, text).group(1))

    def after(hours):
        return (now + timedelta(hours=hours)).isoformat()

    def held(reason):
        ((part, hold),) = [(p["material_id"], h) for p in base["parts"] for h in p["holds"] if h["reason"] == reason]
        return part, hold

    timing = FIXTURE["timing"]
    short_part, _ = held("disputed_receipt")
    failed_part, lot = held("failed_inspection")
    overrun = {"kind": "offer", "offer": {
        "offer_id": "overrun", "supplier_id": "scenario-supplier", "part_id": short_part,
        "quantity": quantity(r"\+([\d,]+) only if"),
        "available_at": after(timing["offer_lands_after_h"]), "valid_until": after(timing["offer_valid_h"]),
        "sources": [{"scenario_input": "overrun offer in the hand analysis"}]}}
    concession = {"kind": "conditional_release", "part_id": failed_part, "batch": lot["batch"],
                  "quantity": quantity(r"about ([\d,]+) good"),
                  "available_at": after(timing["release_after_h"]),
                  "sources": [{"scenario_input": "sorted lot estimate in the hand analysis"}]}
    return facts, now, deadline, base, {"A": [], "B": [overrun], "C": [overrun, concession]}


@pytest.mark.parametrize("name", ["A", "B", "C"])
def test_reference_scenario(world, name):
    facts, now, deadline, base, scenarios = world
    result = simulate.simulate(facts, scenarios[name], now=now, deadline=deadline)
    contracts.validate("coverage", result)
    assert all(a["feasible"] for a in result.get("actions", []))
    problem = difference(facts, base, result, FIXTURE["packed_in_policy_order"][name])
    assert not problem, problem
    if name == "A":
        assert result == base


def test_a_miss_reports_the_delta_and_the_facts_that_moved(world, monkeypatch):
    facts, now, deadline, base, _ = world
    first, second = FIXTURE["packed_in_policy_order"]["A"]
    was = dict(FIXTURE["facts"], gone_fact="stock_batch.qty")
    del was[base["sources"][0]["fact_id"]]
    monkeypatch.setitem(FIXTURE, "facts", was)
    problem = difference(facts, base, base, [first + 7, second])
    assert "delta [-7, 0]" in problem
    assert "gone  gone_fact stock_batch.qty" in problem
    assert f"new   {base['sources'][0]['fact_id']}" in problem


def test_an_expired_reference_offer_is_infeasible(world):
    facts, now, deadline, base, scenarios = world
    offer = scenarios["B"][0]["offer"]
    action = {"kind": "offer", "offer": {**offer, "valid_until": now.isoformat()}}
    result = simulate.simulate(facts, [action], now=now, deadline=deadline)
    assert result["actions"][0]["reason"] == "expired"
    assert result["covered"] == base["covered"]
