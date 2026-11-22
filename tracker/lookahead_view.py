"""What the page shows of the records, and what it needs on top, so that it only renders. Pure functions.

The goal bar comes from the coverage of the latest plan record. The ghost bar is the counterfactual arrival
that the plan record carries (the timing model's `ghost`): it is drawn next to the bar it is compared with and
is `late` when the model says so or it lands after the lane's needed-by time or the launch deadline. No arrival
is computed here.
"""

from __future__ import annotations

from datetime import datetime


def shown_live(record: dict) -> bool:
    """Live mode shows what an agent or the monitor produced, and the human steps taken on the page."""
    if record["kind"] == "clock" or (record["kind"] == "launch" and record.get("mode") == "rehearse"):
        return False
    producer = str(record.get("producer") or "")
    return producer in ("agent", "monitor", "app") or producer.startswith("agent:")


def _time(value) -> datetime | None:
    try:
        at = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return at if at.tzinfo else None


def coverage_view(cov) -> dict | None:
    """Coverage with `fraction`, `met` and `hedged` always set. Never met while short of what is needed."""
    if not isinstance(cov, dict):
        return None
    covered, needed = cov.get("covered"), cov.get("needed")
    if not all(isinstance(v, (int, float)) for v in (covered, needed)):
        return None
    hedged = bool(cov.get("hedged"))
    met = covered >= needed and bool(cov.get("met", not hedged))
    return {**cov, "unit": cov.get("unit", ""), "met": met, "hedged": hedged,
            "fraction": min(1.0, max(0.0, covered / needed)) if needed > 0 else 1.0}


def ghost_view(bar: dict, needed_by, deadline) -> dict:
    """The ghost with `late`, and the limit it misses (the deadline when it misses both)."""
    lands = _time(bar.get("usable_at"))
    missed = [limit for limit in (deadline, needed_by) if lands and _time(limit) and lands > _time(limit)]
    late = bool(missed or bar.get("misses_deadline") or bar.get("offer_expired") or bar.get("in_time") is False)
    return {"id": "ghost", "label": "If started now", **bar, "late": late, "late_after": missed[0] if missed else None}


def lane_view(lane: dict, deadline) -> dict:
    bars = lane.get("bars") or []
    out = [b for b in bars if b.get("kind") != "ghost"]
    for ghost in (b for b in bars if b.get("kind") == "ghost"):
        # directly under the live bar of the same supplier (a ghost without one: under the hedge), else last
        key = "supplier_id" if ghost.get("supplier_id") else "kind"
        want = ghost.get("supplier_id") or "hedge"
        pos = next((i + 1 for i, b in enumerate(out) if b.get("kind") != "ghost" and b.get("state") != "failed"
                    and b.get(key) == want), len(out))
        out.insert(pos, ghost_view(ghost, lane.get("needed_by"), deadline))
    return {**lane, "name": lane.get("name") or lane.get("lane_id"), "bars": out}


def plan_view(plan: dict | None, launch: dict | None) -> dict | None:
    if plan is None:
        return None
    deadline = (launch or {}).get("deadline")
    return {**plan, "coverage": coverage_view(plan.get("coverage")),
            "lanes": [lane_view(lane, deadline) for lane in plan.get("lanes") or []]}
