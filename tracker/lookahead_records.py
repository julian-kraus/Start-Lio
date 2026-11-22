"""What one Lookahead record must be before it is written to the log or folded from it."""

from __future__ import annotations

import json

KINDS = {"launch", "clock", "goal", "plan", "supplier", "event", "decision", "promise", "cue"}
GOAL_AMOUNTS = ("unit_loss_eur", "budget_cap_eur", "budget_eur")
UPSERT_KEYS = {"supplier": "supplier_id", "decision": "decision_id", "promise": "promise_id"}


class LookaheadError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def validate(record: dict) -> dict:
    if not isinstance(record, dict):
        raise LookaheadError(400, "record must be a JSON object")
    kind = record.get("kind")
    if kind not in KINDS:
        raise LookaheadError(400, f"unknown kind {kind!r}")
    key = UPSERT_KEYS.get(kind)
    if key and not record.get(key):
        raise LookaheadError(400, f"{kind} record needs {key}")
    if kind == "event" and not (record.get("type") and record.get("title")):
        raise LookaheadError(400, "event record needs type and title")
    if kind == "goal":
        _check_goal(record)
    try:
        json.dumps(record, allow_nan=False)
    except ValueError:  # NaN and infinity: a browser cannot parse a state that holds one
        raise LookaheadError(400, "numbers must be finite") from None
    return record


def _check_goal(record: dict) -> None:
    """What record_goal refuses, checked here because the screen also runs without the plan package."""
    if "statement" in record and not isinstance(record["statement"], str):
        raise LookaheadError(400, "statement must be text")
    for key in GOAL_AMOUNTS:
        value = record.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0):
            raise LookaheadError(400, f"{key} must be a number of 0 or more")


def one_budget_name(record: dict) -> dict:
    """Goals are written with budget_cap_eur, the name of the plan.goals column."""
    if record["kind"] != "goal" or "budget_eur" not in record:
        return record
    out = {k: v for k, v in record.items() if k != "budget_eur"}
    if out.setdefault("budget_cap_eur", record["budget_eur"]) != record["budget_eur"]:
        raise LookaheadError(400, "budget_eur and budget_cap_eur differ")
    return out
