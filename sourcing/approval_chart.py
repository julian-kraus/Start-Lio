"""Who holds which seat, from the `org_seat` facts of an org chart.

Extraction says who is in a seat and whether it is held (contract fact type `org_seat`). This module never decides
from the shape of a line that it is a person: the lines of a chart give only its version, its date and citations.
A seat is `held` only if its facts say exactly that, name a holder, agree and can be read. A seat that breaks
those rules is `unclear`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from sourcing.approval_docs import CHART_KIND, MIN_CONFIDENCE, Document, iso_date, ours, words

SEAT_TYPE = "org_seat"
HELD = "held"
STATUSES = (HELD, "vacant", "absent", "unclear")
ATTRIBUTES = ("holder", "status", "valid_from", "valid_until", "line_key")
SENIORITY = {"senior", "junior", "principal", "acting"}


@dataclass
class Seat:
    title: str
    scope: str
    name: str | None               # only when the state is held
    state: str                     # one of STATUSES
    start: date | None
    end: date | None
    line_key: str | None
    ids: list[str]

    @property
    def label(self) -> str:
        """The title with its scope, which extraction may keep out of the title."""
        in_title = set(words(self.scope)) <= set(words(self.title))
        return self.title if in_title else f"{self.title} {self.scope}"

    def state_on(self, day: date) -> str:
        if self.state != HELD:
            return self.state
        if self.start and day < self.start:
            return "not_started"
        return "ended" if self.end and self.end < day else HELD


@dataclass
class Chart:
    path: str
    sha256: str
    version: str | None
    dated: date | None
    problems: list[str]
    ids: list[str]
    seats: list[Seat] = field(default_factory=list)


def title_scope(title: str, term: str) -> str | None:
    """None when the title is not this role; otherwise what the title is limited to, "" for the whole company."""
    paren = re.fullmatch(r"(.*?)\s*\((.*)\)", title.strip())
    base = words(paren[1] if paren else title)
    while base and base[0] in SENIORITY:
        base = base[1:]
    wanted = words(term)
    if re.fullmatch(r"C[A-Z]O", term.strip()):
        if len(base) < 3 or base[0] != "chief" or base[2] != "officer" or base[1][0] != term.strip()[1].lower():
            return None
        size = 3
    elif wanted and base[:len(wanted)] == wanted:
        size = len(wanted)
    else:
        return None
    return " ".join(base[size:] + (words(paren[2]) if paren else []))


def seat_scope(seat: Seat, role: str) -> str | None:
    """What the seat is limited to, by its title and its scope key together. Neither can widen the other."""
    in_title = title_scope(seat.title, role)
    if in_title is None:
        return None
    printed = words(seat.scope)
    titled = in_title.split()
    if set(titled) <= set(printed):
        return " ".join(printed)
    return in_title if set(printed) <= set(titled) else " ".join(titled + printed)


def read_seats(rows: list[dict]) -> dict[tuple[str, str], list[Seat]]:
    """The seats of our own org charts by (path, sha256), one per extractor, title and scope."""
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        if row.get("fact_type") != SEAT_TYPE or row.get("doc_kind") != CHART_KIND or not ours(row):
            continue
        subject = row.get("subject") or {}
        key = (row.get("path") or "", row.get("sha256") or "", row.get("extractor") or "",
               str(subject.get("title") or ""), str(subject.get("scope") or ""))
        groups.setdefault(key, []).append(row)
    seats: dict[tuple[str, str], list[Seat]] = {}
    for (path, sha, _, title, scope), facts in sorted(groups.items()):
        seats.setdefault((path, sha), []).append(_seat(title, scope, facts))
    return seats


def _seat(title: str, scope: str, facts: list[dict]) -> Seat:
    values: dict[str, str] = {}
    readable = True
    for fact in facts:
        name, value = fact.get("attribute"), fact.get("value")
        if name not in ATTRIBUTES:
            continue
        value = "" if value is None else value
        low = fact.get("confidence") is not None and float(fact["confidence"]) < MIN_CONFIDENCE
        if low or not isinstance(value, str) or values.setdefault(name, value.strip()) != value.strip():
            readable = False        # low confidence, not text, or two values for one attribute
    start, end = iso_date(values.get("valid_from")), iso_date(values.get("valid_until"))
    if (values.get("valid_from") and not start) or (values.get("valid_until") and not end):
        readable = False
    if start and end and end < start:
        readable = False
    status, holder = values.get("status"), values.get("holder")
    if not readable or status not in STATUSES or (status == HELD and not holder):
        status = "unclear"
    return Seat(title, scope, holder if status == HELD else None, status, start, end,
                values.get("line_key") or None, sorted(str(f["fact_id"]) for f in facts))


def read_chart(doc: Document) -> Chart:
    """The version and date of a chart, with the line that states them."""
    for line in doc.lines[:4]:
        header = re.search(r"\bv(?:ersion\s*)?(\d+(?:\.\d+)*)\b.*?(\d{4}-\d\d-\d\d)", line.text, re.I)
        if header and (dated := iso_date(header[2])):
            return Chart(doc.path, doc.sha256, header[1], dated, list(doc.problems), [line.fact_id])
    return Chart(doc.path, doc.sha256, None, None, doc.problems + [f"{doc.path}: no version and date"], [])


def read_charts(rows: list[dict], docs: list[Document]) -> tuple[list[Chart], list[str]]:
    """The charts with their seats. Seats of a chart without lines are a problem: it could be the newest chart."""
    seats = read_seats(rows)
    charts = [read_chart(doc) for doc in docs]
    for chart in charts:
        chart.seats = seats.get((chart.path, chart.sha256), [])
    dateless = set(seats) - {(c.path, c.sha256) for c in charts}
    return charts, [f"{path}: seats without the line that dates the chart" for path, _ in sorted(dateless)]


def cited_lines(rows: list[dict], chart: Chart, line_keys: list[str]) -> list[str]:
    """The chart lines the holders were read from."""
    return sorted(str(r["fact_id"]) for r in rows
                  if r.get("fact_type") == "x_document_line" and ours(r)
                  and (r.get("path"), r.get("sha256") or "") == (chart.path, chart.sha256)
                  and (r.get("subject") or {}).get("line_key") in line_keys)
