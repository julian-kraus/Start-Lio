"""Pack windows and line capacity, from the launch plan's shifts and rates and the pack-out log."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from .facts import Facts, day, figure, ids, number

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
SPAN = re.compile(r"(\d{1,2}):(\d{2})\D+(\d{1,2}):(\d{2})")


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime
    full_hours: float       # the whole shift, before it was cut at the deadline
    fact_ids: tuple


def hours(windows: list[Window], since: datetime | None = None, until: datetime | None = None) -> float:
    total = 0.0
    for w in windows:
        start = max(w.start, since) if since else w.start
        end = min(w.end, until) if until else w.end
        total += max(0.0, (end - start).total_seconds() / 3600)
    return total


def pack_windows(facts: Facts, now: datetime, deadline: datetime) -> list[Window]:
    """Shift windows from the day after the last logged pack-out to the deadline.

    Stock and open copies are both as of that log, so the same span is what consumes them.
    """
    logged = [d for r in facts.rows("packout") if (d := day(r.get("date"))) and d < deadline.date()]
    current = max(logged) + timedelta(days=1) if logged else now.astimezone(deadline.tzinfo).date()
    out: dict[tuple, Window] = {}
    shifts = facts.rows("shift")
    while current <= deadline.date():
        for shift in shifts:
            if shift.key("day").lower() not in (WEEKDAYS[current.weekday()], current.isoformat()):
                continue
            span = SPAN.search(shift.key("shift"))
            if not span:
                continue
            h1, m1, h2, m2 = map(int, span.groups())
            midnight = datetime.combine(current, time(), deadline.tzinfo)
            start, end = midnight + timedelta(hours=h1, minutes=m1), midnight + timedelta(hours=h2, minutes=m2)
            if end <= start:
                end += timedelta(days=1)
            full = (end - start).total_seconds() / 3600
            end = min(end, deadline)
            if start < end:
                out[(start, end)] = Window(start, end, full, tuple(shift.ids("shift", "hours")))
        current += timedelta(days=1)
    return sorted(out.values(), key=lambda w: w.start)


def capacity(facts: Facts, edition, windows: list[Window], now: datetime) -> tuple[dict, list[dict]]:
    """Slack of one line or cell, and what makes that slack doubtful."""
    out = {"line": edition.line, "line_status": edition.line_status}
    if not edition.rate or not windows:
        out["flags"] = ["capacity unknown"]
        return out, [{"kind": "capacity_unknown", "product_id": edition.product_id,
                      "fact_ids": ids(edition.open_ids)}]
    rate, shift_ids = edition.rate, ids(*(w.fact_ids for w in windows))
    basis = ids(edition.open_ids, edition.rate_ids)
    needed, available = edition.open / rate, hours(windows)
    remaining, remaining_ids = edition.open, edition.open_ids
    if edition.reported_packed is not None and edition.reported_packed > edition.packed:
        remaining = max(0, edition.demand - edition.reported_packed)
        remaining_ids = ids(edition.demand_ids, edition.reported_ids)
    left = hours(windows, since=now)
    slack_now = left - remaining / rate
    out.update(
        rate_per_hour=figure(rate, edition.rate_ids),
        hours_needed=figure(needed, basis),
        hours_available=figure(available, shift_ids),
        slack_hours=figure(available - needed, basis, shift_ids),
        hours_left=figure(left, shift_ids),
        slack_hours_now=figure(slack_now, remaining_ids, edition.rate_ids, shift_ids),
    )
    flags, alerts = [], []
    per_day: dict = defaultdict(lambda: [0, []])
    for row in facts.rows("packout", product_id=edition.product_id):
        entry = per_day[row.key("date")]
        entry[0] += number(row.get("units_packed")) or 0
        entry[1] += row.ids("units_packed")
    planned_day = rate * max(w.full_hours for w in windows)
    out["planned_best_day"] = figure(planned_day, edition.rate_ids, shift_ids)
    if per_day:
        best, best_ids = max(per_day.values(), key=lambda e: e[0])
        out["best_observed_day"] = figure(best, best_ids)
    else:
        best, best_ids = None, []
    if best is None or planned_day > best:
        flags.append("rate unproven")
        alerts.append({"kind": "rate_unproven", "product_id": edition.product_id,
                       "fact_ids": ids(edition.rate_ids, shift_ids, best_ids)})
    if available < needed or slack_now < 0:
        flags.append("capacity short")
        alerts.append({"kind": "capacity_short", "product_id": edition.product_id,
                       "fact_ids": ids(basis, remaining_ids, shift_ids)})
    out["flags"] = flags
    return out, alerts
