"""Supplier profiles: a deterministic summary per supplier, computed from facts and open issues on every reconcile.

Nothing here interprets status words or knows suppliers: open PO lines are lines without an actual delivery date and
with a positive quantity; lateness is actual minus promised date; everything else is counted as found. Each section
names the fact types it was built from, so the UI can link to the evidence.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import date

from .engine import Issue, Record, normalise

SUPPLIER_IN_PATH = re.compile(r"(?:^|/)suppliers/([^/_]+)_")


def _day(v) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def _num(v) -> float | None:
    return normalise(v, "number")


def supplier_index(records: list[Record]) -> dict[str, dict[str, str]]:
    """Lookup tables from document keys to supplier IDs, built from the facts themselves."""
    idx: dict[str, dict[str, str]] = defaultdict(dict)
    for r in records:
        sup = r.get("supplier_id")
        if not sup:
            continue
        for key in ("po_number", "gr_number", "shipment_id", "reference", "delivery_note"):
            v = r.get(key)
            if v is not None:
                idx[key][str(v)] = str(sup)
    return idx


def supplier_of(r: Record, idx: dict[str, dict[str, str]]) -> str | None:
    if r.get("supplier_id"):
        return str(r.get("supplier_id"))
    m = SUPPLIER_IN_PATH.search(r.source_path)
    if m:
        return m.group(1)
    for key in ("po_number", "gr_number", "batch", "shipment_id", "reference", "delivery_note"):
        v = r.get(key)
        lookup = "gr_number" if key == "batch" else key
        if v is not None and str(v) in idx.get(lookup, {}):
            return idx[lookup][str(v)]
    return None


def build(records: list[Record], issues: list[Issue], today: date) -> dict[str, dict]:
    idx = supplier_index(records)
    by_sup: dict[str, list[Record]] = defaultdict(list)
    fact_owner: dict[str, str] = {}
    for r in records:
        sup = supplier_of(r, idx)
        if sup:
            by_sup[sup].append(r)
            for f in r.attrs.values():
                fact_owner[f.fact_id] = sup
    profiles = {}
    for sup, recs in sorted(by_sup.items()):
        p: dict = {"supplier_id": sup, "sources": dict(Counter(r.fact_type for r in recs))}
        master = next((r for r in recs if r.fact_type == "supplier"), None)
        if master:
            p["master"] = {k: f.value for k, f in master.attrs.items()}
        p["parts"] = [{"material_id": r.get("material_id"), **{k: f.value for k, f in r.attrs.items()}}
                      for r in recs if r.fact_type == "supplier_part"]
        p["contract"] = [{**r.subject, **{k: f.value for k, f in r.attrs.items()}}
                         for r in recs if r.fact_type == "contract_term"]
        p["delivery"] = _delivery(recs, today)
        p["statements"] = dict(Counter(str(r.get("kind")) for r in recs if r.fact_type == "supplier_statement"))
        p["shipments"] = dict(Counter(str(r.get("status")) for r in recs if r.fact_type == "shipment"))
        p["price_changes"] = [{**r.subject, **{k: f.value for k, f in r.attrs.items()}}
                              for r in recs if r.fact_type == "price_change"]
        p["issues"] = _issues(sup, issues, fact_owner)
        profiles[sup] = p
    return profiles


def _delivery(recs: list[Record], today: date) -> dict:
    delivered = late = 0
    delay_days = []
    open_lines = 0
    open_value: dict[str, float] = defaultdict(float)
    next_promised = None
    for r in recs:
        if r.fact_type != "po_line":
            continue
        promised, actual, qty = _day(r.get("promised_date")), _day(r.get("actual_delivery_date")), _num(r.get("qty"))
        if actual and promised:
            delivered += 1
            d = (actual - promised).days
            if d > 0:
                late += 1
                delay_days.append(d)
        elif actual is None and qty and qty > 0:
            open_lines += 1
            price = _num(r.get("unit_price"))
            if price is not None:
                open_value[str(r.get("currency") or "?")] += qty * price
            if promised and promised >= today and (next_promised is None or promised < next_promised):
                next_promised = promised
    return {
        "delivered_lines": delivered, "late_lines": late,
        "late_share": round(late / delivered, 3) if delivered else None,
        "avg_delay_days_when_late": round(sum(delay_days) / len(delay_days), 1) if delay_days else None,
        "max_delay_days": max(delay_days) if delay_days else None,
        "open_lines": open_lines, "open_value": {k: round(v, 2) for k, v in open_value.items()},
        "next_promised_date": next_promised.isoformat() if next_promised else None,
    }


def _issues(sup: str, issues: list[Issue], fact_owner: dict[str, str]) -> dict:
    mine = [i for i in issues if any(fact_owner.get(fid) == sup for fid in i.fact_ids)]
    return {
        "open": len(mine),
        "by_severity": dict(Counter(i.severity for i in mine)),
        "by_rule": dict(Counter(i.payload.get("rule_id", i.kind) for i in mine)),
        "top": [{"issue_key": i.issue_key, "severity": i.severity, "title": i.title}
                for i in sorted(mine, key=lambda i: ("high", "cost", "medium", "low", "info").index(i.severity)
                                if i.severity in ("high", "cost", "medium", "low", "info") else 9)[:5]],
    }
