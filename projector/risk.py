"""The plan record: what each part can still pack, and the parts that can break the goal.

`sourcing.coverage` computes covered, needed and gap with their fact ids; `sourcing.weakspots` ranks the parts
by exposure and gives each a status and a reason valued against the goal. Both are deterministic for a `Facts`,
a clock and a deadline. The projector folds them into one Lookahead `plan` record: one lane per part, every
number a figure with its fact ids.
"""

from __future__ import annotations

from datetime import datetime

from sourcing import coverage as coverage_model
from sourcing import weakspots
from sourcing.facts import Facts


def coverage_of(facts: Facts, *, now: datetime, deadline: datetime, products=None) -> dict:
    """Coverage for the launch, deterministic for these facts, clock and deadline."""
    return coverage_model.compute(facts, now=now, deadline=deadline, products=products)


def weak_spots(facts: Facts, coverage: dict, goal: dict | None) -> list[dict]:
    """The parts that can break the goal, worst first; empty when there is no goal to value the editions with."""
    if not goal:
        return []
    return weakspots.rank(facts, coverage, goal)


def plan_record(coverage: dict, spots: list[dict]) -> dict:
    """One `plan` record. Each lane carries the part's coverage figures and, from its weak spot, the status,
    reason and exposure. The lane-level numbers stay figures (value plus fact ids) so each keeps its source."""
    by_part = {s["part_id"]: s for s in spots}
    lanes = []
    for part in coverage["parts"]:
        pid = part["material_id"]
        lane = {"lane_id": pid, "need": part["need"]}
        for field in ("available", "gap", "gap_on_stock", "inbound_counted"):
            if field in part:
                lane[field] = part[field]
        spot = by_part.get(pid)
        if spot:
            detail = spot["detail"]
            lane["status"] = detail["status"]
            lane["reason"] = spot["reason"]
            lane["exposure_eur"] = {"value": spot["exposure_eur"], "sources": spot["sources"]}
            lane["copies_at_risk"] = detail["copies_at_risk"]
            lane["money_at_risk_eur"] = detail["money_at_risk_eur"]
        lanes.append(lane)
    lanes.sort(key=lambda lane: lane["lane_id"])
    return {
        "kind": "plan",
        "at": coverage["at"],
        "lanes": lanes,
        "coverage": {"covered": coverage["covered"], "needed": coverage["needed"], "gap": coverage["gap"],
                     "unit": coverage["unit"], "fact_ids": coverage["fact_ids"]},
        "sources": coverage["sources"],
    }
