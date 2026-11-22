"""Timing chain: when a supply option lands, and until when it is worth starting. Pure functions, no clock, no DB.

    chain = Chain(load=offer_leg("load", "within 4 h", src),    # supplier promise
                  transit=route_leg(facts, route_id),           # or offer_leg("transit", text, src)
                  last_mile=last_mile_leg(facts, route_id, site),
                  receiving=Leg("receiving", hours, src))
    assess(chain, now=..., needed_at=..., needed_source=..., start=..., deadline=..., team=team_window(goal))

  decide_by    needed-at minus lead time.
  too_late_at  starting after this fails: decide_by, or earlier when the offer expires first.
  act_by       the last moment from now up to too_late_at at which the team can be asked (team window from the goal).
  ghost        the same chain started at `now`, whenever it was really started: the counterfactual arrival.

Every leg needs a source and none has a default: a leg that does not exist is an explicit 0 h with the reason as its
source. `now` is passed in (plan.clock.now). Times out are ISO 8601 in the zone of `needed_at`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from plan import clock

NUMBER = r"(?:\d{1,3}(?:[   ]\d{3})+|\d+)(?:[.,]\d{1,2})?"
UNIT_HOURS = {1 / 60: "min mins minute minutes minuten minut minuty",
              1: "h hr hrs hour hours std stunde stunden godz godzin godziny godzinę",
              24: "d day days tag tage tagen dni dzień"}
UNITS = {word: hours for hours, words in UNIT_HOURS.items() for word in words.split()}
QUALIFIER = re.compile(r"^(?:(?:within|in|about|approx\.?|approximately|up to|max\.?|ca\.?|circa|binnen|innerhalb von|"
                       r"w ciągu|ok\.|do)(?=[\s\d])|~|≤|<=)\s*")
PART = re.compile(rf"({NUMBER})(?:\s*(?:-|–|to|bis)\s*({NUMBER}))?\s*([^\W\d_]+)\.?\s*")
MAX_LEG_H = 10 * 366 * 24     # beyond ten years it is a misread number, and date arithmetic overflows
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class TimingError(ValueError):
    pass


def instant(value: datetime | str) -> datetime:
    """An aware time. A naive one is refused, it hides the offset."""
    if isinstance(value, datetime):
        if value.utcoffset() is None:
            raise TimingError(f"time needs a UTC offset: {value!r}")
        return value
    try:
        return clock.parse(value)
    except clock.ClockError as exc:
        raise TimingError(str(exc)) from exc


def _utc(value: datetime | str) -> datetime:
    # all arithmetic is in UTC: adding to a zoned time moves the wall clock and is wrong across a DST change
    return instant(value).astimezone(timezone.utc)


def _number(text: str) -> float:
    return float(re.sub(r"\s", "", text).replace(",", "."))


def parse_hours(text: str) -> float:
    """Hours in a duration quoted verbatim, such as "within 4 h", "2,5 Std." or "1 h 30 min".

    A range counts at its slow end. Anything not fully understood ("2 working days", "tomorrow", a bare number) is
    refused, never guessed.
    """
    if not isinstance(text, str):
        raise TimingError(f"duration must be quoted text: {text!r}")
    rest = QUALIFIER.sub("", text.strip().lower())
    total, pos = 0.0, 0
    while m := PART.match(rest, pos):
        low, high, unit = m.groups()
        if unit not in UNITS:
            break
        amount = _number(high or low)
        if high and amount < _number(low):
            raise TimingError(f"range runs backwards: {text!r}")
        total += amount * UNITS[unit]
        pos = m.end()
    if not pos or pos != len(rest):
        raise TimingError(f"cannot read a duration from {text!r}")
    return total


@dataclass(frozen=True)
class Leg:
    label: str
    hours: float
    source: str | dict

    def __post_init__(self):
        h = self.hours
        if isinstance(h, bool) or not isinstance(h, (int, float)) or not 0 <= h <= MAX_LEG_H:
            raise TimingError(f"{self.label}: hours must be between 0 and {MAX_LEG_H}, got {h!r}")
        if not isinstance(self.source, (str, dict)) or not self.source:
            raise TimingError(f"{self.label}: needs a source")

    @property
    def delta(self) -> timedelta:
        return timedelta(seconds=round(self.hours * 3600))


def offer_leg(label: str, text: str, source: str | dict) -> Leg:
    """A leg from the wording of an offer or a supplier statement."""
    return Leg(label, parse_hours(text), source)


def load_by_leg(due: datetime | str, ordered_at: datetime | str, source: str | dict) -> Leg:
    """A load promise given as a time. A counterfactual start reuses the same duration."""
    hours = (_utc(due) - _utc(ordered_at)).total_seconds() / 3600
    if hours < 0:
        raise TimingError("load promise is before the order")
    return Leg("load", hours, source)


def _routes(facts: Iterable[Mapping]) -> dict[str, dict[str, list[Mapping]]]:
    routes: dict[str, dict[str, list[Mapping]]] = {}
    for f in facts:
        route_id = (f.get("subject") or {}).get("route_id")
        if f.get("fact_type") == "route" and route_id is not None and f.get("value") is not None:
            routes.setdefault(str(route_id), {}).setdefault(f["attribute"], []).append(f)
    return routes


def _values(route: dict[str, list[Mapping]], attribute: str) -> set[str]:
    return {str(f["value"]) for f in route.get(attribute, [])}


def _fact_source(rows: list[Mapping]) -> str | dict:
    ids = sorted({str(f["fact_id"]) for f in rows if f.get("fact_id")})
    if not ids:
        raise TimingError("route fact without a fact_id")
    return {"fact_id": ids[0]} if len(ids) == 1 else {"fact_id": ids[0], "fact_ids": ids}


def route_leg(facts: Iterable[Mapping], route_id: str, label: str = "transit") -> Leg:
    """Transit from `kb.current_facts` rows. Differing facts: the slower one counts and all are cited."""
    rows = _routes(facts).get(str(route_id), {}).get("transit_days")
    if not rows:
        raise TimingError(f"no transit_days fact for route {route_id!r}")
    days = []
    for f in rows:
        text = str(f["value"]).strip()
        if not re.fullmatch(NUMBER, text):
            raise TimingError(f"transit_days is not a number: {text!r}")
        days.append(_number(text))
    return Leg(label, max(days) * 24, _fact_source(rows))


def last_mile_leg(facts: Iterable[Mapping], route_id: str, site: str) -> Leg:
    """The route from where `route_id` ends to the site. 0 h only when a fact says the route ends at the site."""
    facts = list(facts)
    routes = _routes(facts)
    main = routes.get(str(route_id), {})
    ends = _values(main, "destination")
    if len(ends) != 1:
        raise TimingError(f"route {route_id!r} needs exactly one destination, found {sorted(ends)}")
    if ends == {site}:
        return Leg("last mile", 0, _fact_source(main["destination"]))
    onward = [route_leg(facts, rid, "last mile") for rid, r in sorted(routes.items())
              if _values(r, "origin") == ends and _values(r, "destination") == {site}]
    if not onward:
        raise TimingError(f"no route fact from {sorted(ends)} to {site!r}")
    return max(onward, key=lambda leg: leg.hours)


@dataclass(frozen=True)
class Chain:
    load: Leg
    transit: Leg
    last_mile: Leg
    receiving: Leg

    @property
    def legs(self) -> tuple[Leg, ...]:
        return (self.load, self.transit, self.last_mile, self.receiving)

    @property
    def lead_time(self) -> timedelta:
        return sum((leg.delta for leg in self.legs), timedelta())

    @property
    def lead_time_h(self) -> float:
        return self.lead_time.total_seconds() / 3600

    @property
    def sources(self) -> list:
        return [leg.source for leg in self.legs]


def arrival(chain: Chain, start: datetime | str) -> datetime:
    start = instant(start)
    return (_utc(start) + chain.lead_time).astimezone(start.tzinfo)


def decide_by(chain: Chain, needed_at: datetime | str) -> datetime:
    needed = instant(needed_at)
    return (_utc(needed) - chain.lead_time).astimezone(needed.tzinfo)


def too_late_at(chain: Chain, needed_at: datetime | str, valid_until: datetime | str | None = None) -> datetime:
    """Starting after this moment cannot work: the goods arrive late or the offer has expired."""
    limit = decide_by(chain, needed_at)
    if valid_until is not None and _utc(valid_until) < _utc(limit):
        return instant(valid_until).astimezone(limit.tzinfo)
    return limit


def timeline(chain: Chain, start: datetime | str) -> dict:
    """The part of a plan bar that timing owns: start, usable_at and the legs, each with its source."""
    start = instant(start)
    at, legs = _utc(start), []
    for leg in chain.legs:
        end = at + leg.delta
        legs.append({"label": leg.label, "from": at.astimezone(start.tzinfo).isoformat(),
                     "to": end.astimezone(start.tzinfo).isoformat(), "hours": leg.hours, "source": leg.source})
        at = end
    return {"start": start.isoformat(), "usable_at": at.astimezone(start.tzinfo).isoformat(), "legs": legs}


@dataclass(frozen=True)
class Window:
    start: datetime | None      # None: open since before anything we plan
    end: datetime | None        # None: no end given
    source: str | dict

    def __post_init__(self):
        for value in (self.start, self.end):
            if value is not None:
                instant(value)
        if self.start and self.end and _utc(self.end) <= _utc(self.start):
            raise TimingError("window ends before it starts")


def team_window(goal: Mapping) -> Window | None:
    """When the team can be asked, from a goal record; None when the goal does not say.

    Read from `team_window`, on the record or among its answers (object, or list of {id, a|answer|value}).
    The value is {"from"?: time, "to"?: time}, or one time meaning "reachable until then".
    """
    value = goal.get("team_window")
    answers = goal.get("answers") or {}
    if value is None and isinstance(answers, Mapping):
        value = answers.get("team_window")
    elif value is None:
        for item in answers:
            if isinstance(item, Mapping) and item.get("id") == "team_window":
                value = next((item[k] for k in ("a", "answer", "value") if item.get(k) is not None), None)
    if value is None or value == "":
        return None
    source = {"goal_line": "team_window", "value": value}
    if isinstance(value, str):
        return Window(None, instant(value), source)
    if not isinstance(value, Mapping) or not (value.get("from") or value.get("to")):
        raise TimingError(f"cannot read a team window from {value!r}")
    start, end = value.get("from"), value.get("to")
    return Window(instant(start) if start else None, instant(end) if end else None, source)


def act_by(window: Window, now: datetime | str, limit: datetime | str) -> datetime | None:
    """The latest moment in the window from now up to the limit; None when nobody can be asked in time."""
    lo, hi = _utc(now), _utc(limit)
    if window.start:
        lo = max(lo, _utc(window.start))
    if window.end:
        if lo >= _utc(window.end):
            return None
        hi = min(hi, _utc(window.end))
    return hi.astimezone(instant(limit).tzinfo) if lo <= hi else None


def next_office_slot(now: datetime | str, hours: Mapping) -> Window:
    """The open or next office-hours slot; inside one it starts now.

    hours = {"timezone": IANA name, "days": ["mon", ...], "from": "HH:MM", "to": "HH:MM", "source": ...}
    """
    try:
        zone = ZoneInfo(hours["timezone"])
        days = {d.lower() for d in hours["days"]}
        opens, closes = time.fromisoformat(hours["from"]), time.fromisoformat(hours["to"])
        source = hours["source"]
    except (KeyError, TypeError, AttributeError, ValueError, OSError, ZoneInfoNotFoundError) as exc:
        raise TimingError(f"bad office hours: {exc!r}") from exc
    if not days or not days <= set(WEEKDAYS) or closes <= opens or not source:
        raise TimingError("office hours need weekdays, an opening before the closing, and a source")
    at = _utc(now)
    today = at.astimezone(zone).date()
    for offset in range(8):     # today, then one full week
        day = today + timedelta(days=offset)
        if WEEKDAYS[day.weekday()] not in days:
            continue
        end = datetime.combine(day, closes, zone)
        if at < _utc(end):
            start = max(at, _utc(datetime.combine(day, opens, zone)))
            return Window(start.astimezone(zone), end, source)
    raise TimingError("no office-hours slot within a week")


def assess(chain: Chain, *, now: datetime | str, needed_at: datetime | str, needed_source: str | dict,
           start: datetime | str | None = None, deadline: datetime | str | None = None,
           deadline_source: str | dict | None = None, valid_until: datetime | str | None = None,
           valid_until_source: str | dict | None = None, team: Window | None = None,
           office_hours: Mapping | None = None) -> dict:
    """The whole timing answer for one option, JSON-ready, plus the ghost started at `now`.

    `start` is when the option was or will be started (default now). `sources` covers every input used.
    """
    needed = instant(needed_at)
    tz = needed.tzinfo
    sources = [*chain.sources, needed_source]
    for name, value, source in (("needed_at", needed, needed_source), ("deadline", deadline, deadline_source),
                                ("valid_until", valid_until, valid_until_source)):
        if value is not None and not source:
            raise TimingError(f"{name} needs a source")
    sources += [s for s in (deadline_source, valid_until_source) if s]
    end = instant(deadline) if deadline is not None else needed

    def option(begun: datetime | str) -> dict:
        begun = instant(begun).astimezone(tz)
        usable = _utc(arrival(chain, begun))
        expired = valid_until is not None and _utc(begun) > _utc(valid_until)
        return {**timeline(chain, begun), "in_time": not expired and usable <= _utc(needed),
                "misses_deadline": usable > _utc(end), "offer_expired": expired,
                "late_by_h": max(0.0, (usable - _utc(needed)).total_seconds() / 3600)}

    limit = too_late_at(chain, needed, valid_until)
    out = {"lead_time_h": chain.lead_time_h, "needed_at": needed.isoformat(),
           "decide_by": decide_by(chain, needed).isoformat(),
           "decide_by_why": f"needed {needed:%a %H:%M} minus {chain.lead_time_h:.4g} h lead time ("
                            + ", ".join(f"{leg.label} {leg.hours:.4g} h" for leg in chain.legs) + ")",
           "too_late_at": limit.isoformat(), "too_late": _utc(now) > _utc(limit),
           "planned": option(now if start is None else start),
           "ghost": {**option(now), "kind": "ghost", "state": "counterfactual"},
           "team_window": None, "act_by": None, "office_slot": None}
    if deadline is not None:
        out["deadline"] = end.astimezone(tz).isoformat()
    if team:
        last = act_by(team, now, limit)
        out["team_window"] = {"from": team.start.astimezone(tz).isoformat() if team.start else None,
                              "to": team.end.astimezone(tz).isoformat() if team.end else None,
                              "source": team.source}
        out["act_by"] = last.isoformat() if last else None
        sources.append(team.source)
    if office_hours is not None:
        slot = next_office_slot(now, office_hours)
        out["office_slot"] = {"from": slot.start.astimezone(tz).isoformat(), "to": slot.end.astimezone(tz).isoformat(),
                              "in_time": _utc(slot.start) <= _utc(limit), "source": slot.source}
        sources.append(slot.source)
    out["sources"] = [s for i, s in enumerate(sources) if s not in sources[:i]]
    return out
