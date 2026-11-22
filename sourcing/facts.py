"""Read-only queries over what kb believes: current facts, believed values and open issues.

`load(conn)` is the only function that touches a database. Everything else works on a `Facts` value, so the
models in sourcing/ stay pure and can be tested on plain rows.

A figure is a number with the facts it was computed from: {"value": 120, "fact_ids": ["..."]}.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Iterable

from psycopg import sql
from psycopg.rows import dict_row

DERIVED = "derived:"
GROUPED = re.compile(r"[+-]?\d{1,3}(,\d{3})+(\.\d+)?")


def number(value) -> int | float | None:
    """A number as written in a fact. Space and comma grouped thousands are accepted, anything else is None.

    A dot is always a decimal point, so "2.000" is 2: thousands grouped with dots must be normalised in kb.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        n = float(value)
    else:
        text = re.sub(r"\s", "", str(value))
        if GROUPED.fullmatch(text):
            text = text.replace(",", "")
        if not re.fullmatch(r"[+-]?\d+(\.\d+)?", text):
            return None
        n = float(text)
    return int(n) if n.is_integer() else n


def day(value) -> date | None:
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def moment(value) -> datetime | None:
    """The time in a dated value, None when it carries only a date. A time that cannot be read is the day's end."""
    text, on = str(value).strip(), day(value)
    if on is None or ":" not in text:
        return None
    try:
        at = datetime.fromisoformat(text)
    except ValueError:
        return datetime.combine(on, time(23, 59, 59))
    return None if at.tzinfo is None and at.time() == time() else at            # an exported date


def id_list(value) -> list[str]:
    """Ids kept either as a list or as one delimited text."""
    if value in (None, ""):
        return []
    items = value if isinstance(value, list) else re.split(r"[|;,]", str(value))
    return [str(i).strip() for i in items if str(i).strip()]


def ids(*groups: Iterable[str]) -> list[str]:
    """Fact ids in first-seen order. A derived id points at the fact it was derived from."""
    return list(dict.fromkeys(i.removeprefix(DERIVED) for group in groups for i in group if i))


def figure(value, *groups: Iterable[str]) -> dict:
    if isinstance(value, float):
        value = int(value) if value.is_integer() else round(value, 2)
    return {"value": value, "fact_ids": ids(*groups)}


@dataclass
class Row:
    """All facts about one subject from one source document."""
    fact_type: str
    subject: dict
    path: str
    values: dict = field(default_factory=dict)
    sources: dict = field(default_factory=dict)      # attribute -> fact id
    as_of: str = ""

    def get(self, name: str):
        value = self.values.get(name)
        return self.subject.get(name) if value is None else value

    def key(self, name: str) -> str:
        value = self.get(name)
        return "" if value is None else str(value).strip()

    def ids(self, *names: str) -> list[str]:
        """The facts behind these attributes; all of the row when none of them is a fact of its own."""
        found = [self.sources[n] for n in names if n in self.sources]
        return found or list(self.sources.values())


class Facts:
    def __init__(self, current: Iterable[dict], believed: Iterable[dict] = (), issues: Iterable[dict] = ()):
        self.by_id: dict[str, dict] = {}
        grouped: dict[tuple, Row] = {}
        self._rows: dict[str, list[Row]] = defaultdict(list)
        for f in sorted(current, key=lambda f: (f.get("as_of") or "", f.get("confidence") or 0, f["fact_id"])):
            self.by_id[f["fact_id"]] = f
            key = (f["path"], f["fact_type"], tuple(sorted((k, str(v)) for k, v in f["subject"].items())))
            row = grouped.get(key)
            if row is None:
                row = grouped[key] = Row(f["fact_type"], f["subject"], f["path"])
                self._rows[f["fact_type"]].append(row)
            row.values[f["attribute"]] = f["value"]      # the newest, most confident fact is last
            row.sources[f["attribute"]] = f["fact_id"]
            row.as_of = max(row.as_of, f.get("as_of") or "")
        for rows in self._rows.values():
            rows.sort(key=lambda r: (r.path, sorted((k, str(v)) for k, v in r.subject.items())))
        self._believed: dict[str, list[dict]] = defaultdict(list)
        for b in believed:
            self._believed[b["rule_id"]].append(b)
        for values in self._believed.values():
            values.sort(key=lambda b: sorted((k, str(v)) for k, v in b["key"].items()))
        self.issues = list(issues)

    def rows(self, fact_type: str, **where) -> list[Row]:
        return [r for r in self._rows.get(fact_type, [])
                if all(r.key(k) == str(v) for k, v in where.items())]

    def believed(self, rule_id: str, **key) -> list[dict]:
        return [b for b in self._believed.get(rule_id, [])
                if all(str(b["key"].get(k, "")) == str(v) for k, v in key.items())]

    def value_of(self, fact_id: str):
        fact = self.by_id.get(fact_id.removeprefix(DERIVED))
        return fact["value"] if fact else None

    def material(self, name: str) -> str:
        """Our part id for a name that may be a supplier's part number."""
        found = {r.key("material_id") for r in self.rows("part_xref", supplier_part_no=name)} - {""}
        return found.pop() if len(found) == 1 else name


def believed_ids(entry: dict) -> list[str]:
    return ids(entry.get("winning_fact_ids") or [], entry.get("losing_fact_ids") or [])


def load(conn, schema: str | None = None) -> Facts:
    """Read kb through its three public surfaces. Writes nothing."""
    name = schema or os.environ.get("KB_SCHEMA", "kb")

    def read(table: str, columns: str, where: str = "") -> list[dict]:
        query = sql.SQL("SELECT " + columns + " FROM {}.{} " + where).format(
            sql.Identifier(name), sql.Identifier(table))
        with conn.cursor(row_factory=dict_row) as cur:
            return cur.execute(query).fetchall()

    return Facts(
        read("current_facts", "fact_id, path, fact_type, subject, attribute, value, as_of, confidence"),
        read("believed", "rule_id, key, value, winning_fact_ids, losing_fact_ids, contradiction"),
        read("issues", "kind, payload, fact_ids", "WHERE state = 'open'"),
    )
