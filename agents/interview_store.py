"""A confirmed goal: stored in plan.goals with author and time, shown on the timeline, cited by recommendations."""

from __future__ import annotations

from typing import Mapping

from psycopg.types.json import Json

from plan import clock, contracts

from .interview import NOT_A_PERSON, QUESTIONS, ROLE, Draft, InterviewError
from .interview_text import one_line


def build(answers: Mapping[str, str], lines: list[dict]) -> dict:
    """The goal record's content, rebuilt the same way from a draft and from a stored row."""
    of = lambda name: [x for x in lines if x["field"] == name]      # noqa: E731
    prices = {x["edition"]: x["value"] for x in of("value")}
    ceiling = [x["value"] for x in of("ceiling") if x["value"] is not None]
    window = {key: x["value"] for key, name in (("from", "team_from"), ("to", "team_until")) for x in of(name)}
    goal = {"kind": "goal", "statement": one_line(answers.get("goal")), "answers": dict(answers),
            "questions": [{**q, "a": answers.get(q["id"])} for q in QUESTIONS],
            "budget_cap_eur": ceiling[0] if ceiling else None,
            "sacrifices": [x.get("quote") or x["value"] for x in of("trade_off")],
            "edition": None, "unit_loss_eur": None, "goal_lines": lines}
    if len(prices) == 1:        # one price: the form every reader takes, with the edition it is for
        goal["edition"], goal["unit_loss_eur"] = next(iter(prices.items()))
    elif prices:
        goal["unit_loss_by_edition"] = prices
    if window:
        goal["team_window"] = window
    return goal


def confirm(conn, draft: Draft, author: str) -> dict:
    """Store what the person confirmed and supersede the goal before it. Commits. Returns the goal record."""
    by = one_line(author)
    if not by or NOT_A_PERSON.search(by):
        raise InterviewError("a goal is confirmed by a person, by name")
    if not draft.ready:
        raise InterviewError("clear up first: " + "; ".join(draft.problems))
    goal = build(draft.answers, draft.lines)
    contracts.validate_record(goal)
    try:
        deadline = clock.parse(draft.deadline) if draft.deadline else None
        with conn.transaction():
            # one current goal per product, also when two people confirm at once
            conn.execute("LOCK TABLE goals IN SHARE ROW EXCLUSIVE MODE")
            at = clock.now(conn)
            conn.execute("UPDATE goals SET superseded_at = %s "
                         "WHERE superseded_at IS NULL AND product IS NOT DISTINCT FROM %s", (at, draft.product))
            row = conn.execute(
                "INSERT INTO goals (product, statement, deadline, unit_loss_eur, budget_cap_eur, edition, sacrifices, "
                "answers, sources, created_by, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "RETURNING *",
                (draft.product, goal["statement"], deadline, goal["unit_loss_eur"], goal["budget_cap_eur"],
                 goal["edition"], Json(goal["sacrifices"]),
                 Json([{"id": q["id"], "q": q["q"], "a": q["a"]} for q in goal["questions"]]
                      + ([{"id": "team_window", "a": goal["team_window"]}] if "team_window" in goal else [])),
                 Json(draft.lines), by, at)).fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return _record(row)


def current(conn, product: str | None = None) -> dict | None:
    """The goal in force for a product, as a record."""
    row = conn.execute("SELECT * FROM goals WHERE superseded_at IS NULL AND product IS NOT DISTINCT FROM %s "
                       "ORDER BY created_at DESC LIMIT 1", (product,)).fetchone()
    return _record(row) if row else None


def _record(row: Mapping) -> dict:
    answers = {x["id"]: x["a"] for x in row["answers"] if x["id"] != "team_window" and x.get("a")}
    goal = {**build(answers, row["sources"]), "goal_id": str(row["goal_id"]), "author": row["created_by"],
            "at": row["created_at"].isoformat()}
    if row["product"]:
        goal["product"] = row["product"]
    if row["deadline"]:
        goal["deadline"] = row["deadline"].isoformat()
    contracts.validate_record(goal)
    return goal


def records(goal: Mapping) -> list[dict]:
    """What to append to the timeline: the goal and the event that shows who set it.
    Both name their producer: a live screen shows only what an agent, the monitor or the app produced."""
    producer = f"agent:{ROLE}"
    event = {"kind": "event", "producer": producer, "type": "goal", "at": goal["at"], "severity": "info",
             "title": f"Goal set: {goal['statement']}",
             "detail": {"happened": f"{goal['author']} confirmed the goal.",
                        "reasoning": [{"text": x["goal_line"]} for x in goal["goal_lines"]],
                        "autonomy": {"mode": "none", "text": f"Set by {goal['author']}."}}}
    contracts.validate_record(event)
    return [{**goal, "producer": producer}, event]


def cite(goal: Mapping, name: str, edition: str | None = None) -> dict:
    """The source of a number from the goal, as recommendations carry it: the line, its value and who set it."""
    found = [x for x in goal.get("goal_lines") or [] if x["field"] == name
             and (name in ("priority", "trade_off") or x["value"] is not None)]
    if edition is not None:
        wanted = one_line(edition).casefold()
        found = [x for x in found if str(x["edition"]).casefold() in (wanted, "all")]
    if len(found) != 1:
        raise InterviewError(f"the goal has no single {name} line" + (f" for {edition!r}" if edition else ""))
    line = found[0]
    source = {"goal_line": line["goal_line"]}
    if line["value"] is not None:
        source["value"] = line["value"]
    for key in ("goal_id", "author", "at"):
        if goal.get(key):
            source[key] = goal[key]
    if line.get("scenario_input"):
        source["scenario_input"] = line["scenario_input"]
    return source
