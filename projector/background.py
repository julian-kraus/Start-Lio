"""What is already known at the start of a scenario, from kb.supplier_profiles: each supplier's open findings and
its next promised delivery. These are real-data records; the scenario adds nothing to them.
"""
from __future__ import annotations

from datetime import date, datetime, time

from .world import World

SEVERE = ("high",)          # kb issue severities that show on the timeline at the start
PER_SUPPLIER = 1            # findings per supplier lane, worst first, to keep the lanes readable


def _short(text: str, n: int = 52) -> str:
    """The comparison of a kb finding without its reference prefix ("batch 123: A vs B" -> "A vs B")."""
    text = " ".join(str(text).split())
    if ": " in text:
        text = text.split(": ", 1)[1]
    text = text[:1].upper() + text[1:]
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _record_line(delivery: dict) -> list[dict]:
    rows = []
    if delivery.get("delivered_lines"):
        rows.append({"text": "Late order lines", "value": f"{delivery.get('late_lines', 0)} of "
                                                           f"{delivery['delivered_lines']} "
                                                           f"({float(delivery.get('late_share') or 0):.0%})"})
    if delivery.get("open_lines") is not None:
        rows.append({"text": "Open order lines", "value": str(delivery["open_lines"])})
    if delivery.get("max_delay_days") is not None:
        rows.append({"text": "Longest delay", "value": f"{delivery['max_delay_days']} days"})
    return rows


def events(w: World, start: datetime, supplier_ids: list[str], first_part: dict[str, str]) -> list[dict]:
    """Findings known at `start` and next promised deliveries, one record each, every one cited to kb."""
    out = []
    deadline = w.deadline
    for sid in supplier_ids:
        prof = w.profiles.get(sid) or {}
        lane = first_part.get(sid)
        delivery = prof.get("delivery") or {}
        issues = [i for i in ((prof.get("issues") or {}).get("top") or []) if i.get("severity") in SEVERE]
        for issue in issues[:PER_SUPPLIER]:
            out.append({"kind": "event", "type": "document", "at": start.isoformat(),
                        "title": _short(issue["title"]), "lane": lane, "supplier_id": sid, "severity": "risk",
                        "detail": {"happened": f"Open kb finding ({issue['severity']}): {issue['title']}",
                                   "reasoning": _record_line(delivery),
                                   "source": {"label": "kb issue", "issue_key": issue.get("issue_key")},
                                   "autonomy": {"mode": "none", "text": "Known before the scenario starts."}}})
        nxt = delivery.get("next_promised_date")
        if nxt:
            day = date.fromisoformat(str(nxt)[:10])
            at = datetime.combine(day, time(12, 0), w.zone)
            late = at > deadline
            out.append({"kind": "event", "type": "delivery", "at": at.isoformat(),
                        "date_only": True,
                        "title": f"Next promised delivery {day:%d %b}" + (", after the deadline" if late else ""),
                        "lane": lane, "supplier_id": sid, "severity": "info",
                        "detail": {"happened": f"{delivery.get('open_lines', 0)} open order lines; the earliest "
                                               f"promised date is {day:%d %b}.",
                                   "reasoning": _record_line(delivery),
                                   "source": {"label": "kb supplier profile, purchase order lines"},
                                   "autonomy": {"mode": "none", "text": ""}}})
    return out
