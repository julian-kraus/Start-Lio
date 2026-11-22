"""The Agent rules screen: what a person saved, and who may sign what per the kb policy facts.

A `rules` record holds one whole set of rules. The log keeps every save and the latest valid one counts. Only a
save made on the page counts, so an agent cannot hand itself more room by appending a record. Saving changes no
agent behaviour. The signer table is read-only and uses the reader that routes approvals, `sourcing.approvals`.
"""

from __future__ import annotations

import threading
from datetime import date

from .lookahead import Lookahead, LookaheadError

HUMAN_STEP = {"live": ("app",), "rehearse": ("app", "rehearsal")}
SECTIONS = ("autonomy", "hedge", "escalation")
SOURCE = {"label": "Set by a person on the Agent rules page"}
SAVING = threading.Lock()  # the server handles requests in threads
COLUMNS = "fact_id, path, sha256, doc_kind, fact_type, extractor, subject, attribute, value, locator, confidence"


def saved(la: Lookahead) -> dict:
    """The rules in force as saved on the page, None before the first save, and how many saves the log holds."""
    from plan import contracts

    found = [r for r in la.since(0) if r["kind"] == "rules" and r.get("producer") in HUMAN_STEP[la.mode]
             and not contracts.errors("record_rules", r)]
    return {"rules": found[-1] if found else None, "saves": len(found)}


def save(la: Lookahead, body: dict) -> dict:
    """Append one validated record. `based_on` is the id of the rules the page showed, None for none."""
    from plan import contracts

    unknown = set(body) - {*SECTIONS, "based_on"}
    if unknown:
        raise LookaheadError(400, f"unknown fields: {sorted(unknown)}")
    record = {"kind": "rules", "at": la.now(), "source": SOURCE, **{k: body[k] for k in SECTIONS if k in body}}
    problems = contracts.errors("record_rules", record)
    if problems:
        raise LookaheadError(400, "; ".join(problems))
    with SAVING:  # compare and append are one step, or two saves at once both pass the comparison
        current = saved(la)["rules"]
        if body.get("based_on") != (current["id"] if current else None):
            raise LookaheadError(409, "the rules were changed in the meantime, reload the page")
        la.append([record])
    return saved(la)


def signers(rows: list[dict], on: date) -> dict:
    """The policy's value limits with the seats behind each role. A name only where routing would give one."""
    try:
        from sourcing import approvals
        from sourcing.approval_chart import HELD, seat_scope
    except ImportError:
        return unavailable("reader")
    policy, problems = approvals._policy(rows, on)
    if not policy or not policy.tiers:
        return {**unavailable("policy"), "problems": problems}
    chart, _, more = approvals._chart(rows, on)
    problems = problems + more
    out = []
    for tier in policy.tiers:
        for role in tier.roles:
            seats = []
            if chart:
                spellings = policy.spellings(role)
                areas = [area for seat in chart.seats if (area := seat_scope(seat, spellings)) is not None]
                for area in dict.fromkeys(areas):
                    found = approvals._holder(chart, role, spellings, area or None, on)
                    seat = {"state": found["state"], "scope": area, "fact_ids": found.get("fact_ids", [])}
                    if found["state"] == HELD:
                        if problems:
                            seat["state"] = "unclear"
                        else:
                            seat["name"] = found["name"]
                    seats.append(seat)
            out.append({"role": role, "seats": seats, "with": [r for r in tier.roles if r != role],
                        "limit": {"op": tier.op, "value": approvals._plain(tier.amount),
                                  "currency": policy.currency, "fact_ids": tier.ids}})
    return {"available": True, "on": on.isoformat(), "rows": out, "problems": problems,
            "policy": {"name": policy.name, "version": policy.version, "fact_ids": policy.ids}}


def unavailable(reason: str) -> dict:
    return {"available": False, "reason": reason, "rows": []}
