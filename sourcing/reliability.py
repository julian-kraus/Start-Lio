"""Supplier reliability: the chance that a supplier keeps a promise, with every count behind it.

    h = history(facts, believed)              # rows of kb.current_facts and kb.believed
    record = card(h, supplier_id, promises)   # a Lookahead supplier record
    record = update(record, "broken", promise_id)

Pure functions. One order line is one promise, kept when nothing known about it failed: delivered by the promised
date, received in full, no failed inspection. The score is the mean of a Beta posterior that starts at the
all-supplier rate with the weight of k promises, so a thin history stays close to the average. A supplier without
history may start from a labelled scenario prior; real history always wins over one. An inspection report is
identified by its receipt: several documents count as one report, and a failure stands if any of them reports one.
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date

from kb.engine import Fact, Record, build_records, normalise

PRIOR_STRENGTH = 5.0   # model setting: how many promises the starting score is worth
# The words the kb seed rule for inspections maps. Any other decision is counted as unclear, never as a verdict.
FAILED = frozenset({"fail", "failed", "rejected", "reject"})
PASSED = frozenset({"pass", "passed", "accepted"})
OUTCOMES = {"kept": True, "broken": False}
LEDGER_STATES = {"proven": "kept", "broken": "broken"}   # waiting and cancelled say nothing about the supplier
MEASURES = {
    "on_time": ("On time", "PO lines delivered by the promised date", "po_line promised and actual delivery date"),
    "in_full": ("In full", "delivered PO lines received in full", "goods receipts, believed received quantity"),
    "quality": ("Quality", "goods receipts without a failed inspection", "inspection reports on goods receipts"),
}


@dataclass
class Check:
    ok: bool
    fact_ids: list[str]


@dataclass
class SupplierHistory:
    name: str | None = None
    promises: list[dict] = field(default_factory=list)
    checks: dict[str, list[Check]] = field(default_factory=lambda: {m: [] for m in MEASURES})
    not_judged: Counter = field(default_factory=Counter)
    inspections: Counter = field(default_factory=Counter)


@dataclass
class History:
    suppliers: dict[str, SupplierHistory]

    def rate(self, measure: str | None = None) -> tuple[int, int]:
        """(kept, n) over all suppliers, for one measure or for whole promises."""
        if measure is None:
            flags = [p["kept"] for s in self.suppliers.values() for p in s.promises]
        else:
            flags = [c.ok for s in self.suppliers.values() for c in s.checks[measure]]
        return sum(flags), len(flags)


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def _day(value) -> date | None:
    try:
        return date.fromisoformat(_text(value)[:10])
    except ValueError:
        return None


def _number(value) -> float | None:
    n = normalise(value, "number")
    return n if n is not None and math.isfinite(n) else None


def _ids(record: Record, *names: str) -> list[str]:
    return [record.attrs[n].fact_id for n in names if n in record.attrs]


def _records(rows) -> list[Record]:
    facts = [Fact(str(r["fact_id"]), r["fact_type"], r["subject"], r["attribute"], r["value"], r.get("as_of"),
                  r.get("confidence", 1.0), r.get("extractor", ""), r.get("path", ""), r.get("sha256", ""))
             for r in sorted(rows, key=lambda r: str(r["fact_id"]))]
    return build_records(facts)


def _entities(records: list[Record], fact_type: str, *key: str) -> dict[tuple, Record]:
    """One record per entity. The same row in two exports is one promise, not two; the newest value wins."""
    out: dict[tuple, Record] = {}
    for r in records:
        k = tuple(_text(r.get(name)) for name in key)
        if r.fact_type != fact_type or not all(k):
            continue
        merged = out.setdefault(k, Record(fact_type, dict(r.subject), r.source_path, r.source_sha256, r.extractor,
                                          r.doc_kind))
        for name, f in r.attrs.items():
            old = merged.attrs.get(name)
            if old is None or (f.as_of or "", f.fact_id) > (old.as_of or "", old.fact_id):
                merged.attrs[name] = f
    return dict(sorted(out.items()))


def _line_of(receipt: Record, lines: list[tuple[tuple, Record]]) -> tuple | None:
    """The PO line of a receipt. Line numbering differs between exports, so when the number does not fit, the line
    is the only one of that order with the same material, or with the same material and ordered quantity."""
    material = _text(receipt.get("material_id"))
    number = _text(receipt.get("po_line"))
    for key, line in lines:
        if key[1] == number and (not material or _text(line.get("material_id")) in ("", material)):
            return key
    if not material:
        return None
    fits = [(key, line) for key, line in lines if _text(line.get("material_id")) == material]
    if len(fits) > 1:
        ordered = _number(receipt.get("qty_ordered"))
        fits = [(key, line) for key, line in fits if ordered is not None and _number(line.get("qty")) == ordered]
    return fits[0][0] if len(fits) == 1 else None


def _received(receipts: dict[tuple, Record], believed) -> dict[tuple, tuple[float | None, list[str]]]:
    """Quantity per receipt: the kb believed value, so a signed delivery note corrects the booking."""
    def note(record: Record) -> tuple[str, str]:
        return _text(record.get("delivery_note")), _text(record.get("material_id"))

    best = {(_text(b["key"].get("delivery_note")), _text(b["key"].get("material_id"))): b
            for b in believed if b["rule_id"] == "received_qty"}
    shared = Counter(note(r) for r in receipts.values())
    out = {}
    for key, r in receipts.items():
        qty, ids = _number(r.get("qty_received")), _ids(r, "qty_received")
        b = best.get(note(r))
        # A believed value is per note and material; with several receipts on one note it cannot be split.
        if b and shared[note(r)] == 1 and _number(b["value"]) is not None:
            qty, ids = _number(b["value"]), list(b.get("winning_fact_ids") or ids)
        out[key] = (qty, ids)
    return out


def _may_miss_receipt(order: str, material: str, unplaced: set[tuple[str, str]]) -> bool:
    """An unplaced receipt may be this line's unless both name a material and the materials differ."""
    return any(o == order and (not m or not material or m == material) for o, m in unplaced)


def history(facts, believed=()) -> History:
    """Every supplier's promises and checks from PO lines, goods receipts and inspection reports."""
    records = _records(facts)
    lines = _entities(records, "po_line", "po_number", "po_line")
    receipts = _entities(records, "goods_receipt", "gr_number")
    received = _received(receipts, list(believed))
    suppliers: defaultdict[str, SupplierHistory] = defaultdict(SupplierHistory)

    for (sid,), r in _entities(records, "supplier", "supplier_id").items():
        suppliers[sid].name = _text(r.get("name")) or None

    by_order = defaultdict(list)
    for key, line in lines.items():
        by_order[key[0]].append((key, line))
    on_line, unplaced, line_of = defaultdict(list), set(), {}
    for key, r in receipts.items():
        order = _text(r.get("po_number"))
        line_of[key] = _line_of(r, by_order[order]) if order in by_order else None
        if line_of[key]:
            on_line[line_of[key]].append(key)
        elif order:
            unplaced.add((order, _text(r.get("material_id"))))

    owner = {}
    for key, r in receipts.items():
        line = lines.get(line_of[key]) if line_of[key] else None
        owner[key] = _text(r.get("supplier_id")) or (_text(line.get("supplier_id")) if line else "")

    failed: defaultdict[tuple, list[str]] = defaultdict(list)
    reported: defaultdict[tuple, set[str]] = defaultdict(set)
    for r in records:
        if r.fact_type != "inspection_result" or "decision" not in r.attrs:
            continue
        key = next((k for k in ((_text(r.get(n)),) for n in ("gr_number", "batch")) if k in receipts), None)
        if key is None or not owner[key]:
            continue
        word = normalise(r.get("decision"), "text")
        verdict = "failed" if word in FAILED else "passed" if word in PASSED else "unclear"
        reported[key].add(verdict)
        if verdict == "failed":
            failed[key] += _ids(r, "decision")
    for key, verdicts in reported.items():
        worst = next(v for v in ("failed", "passed", "unclear") if v in verdicts)
        suppliers[owner[key]].inspections[worst] += 1

    for key, r in receipts.items():
        if owner[key]:
            ids = _ids(r, "gr_number", "supplier_id") or [f.fact_id for f in r.attrs.values()]
            suppliers[owner[key]].checks["quality"].append(Check(not failed[key], ids + failed[key]))
            if not line_of[key]:
                suppliers[owner[key]].not_judged["receipts without a certain PO line"] += 1

    for key, line in lines.items():
        sid = _text(line.get("supplier_id"))
        if sid:
            _judge_line(key, line, suppliers[sid], on_line[key], received, failed,
                        _may_miss_receipt(key[0], _text(line.get("material_id")), unplaced))
    for key, ids in failed.items():
        if ids and owner[key] and not line_of[key]:
            suppliers[owner[key]].promises.append(
                {"gr_number": key[0], "kept": False, "quality": False, "fact_ids": sorted(set(ids))})
    return History(dict(suppliers))


def _judge_line(key, line, supplier, receipt_keys, received, failed, may_miss_receipt) -> None:
    ordered = _number(line.get("qty"))
    if ordered is not None and ordered <= 0:
        supplier.not_judged["returns and cancelled lines"] += 1
        return
    promised, actual = _day(line.get("promised_date")), _day(line.get("actual_delivery_date"))
    entry = {"po_number": key[0], "po_line": key[1], "promised": promised.isoformat() if promised else None,
             "actual": actual.isoformat() if actual else None, "ordered": ordered}
    ids = _ids(line, "promised_date", "actual_delivery_date")
    verdicts = {}
    if promised and actual:
        verdicts["on_time"] = actual <= promised
        supplier.checks["on_time"].append(Check(verdicts["on_time"], list(ids)))
    qty_ids = _ids(line, "qty") if ordered is not None else []
    ids += qty_ids   # a number on the card cites its fact, judged or not
    quantities = [received[k] for k in receipt_keys]
    if actual and ordered is not None and quantities and all(q is not None for q, _ in quantities):
        total = sum(q for q, _ in quantities)
        entry["received"] = total
        received_ids = [i for _, found in quantities for i in found]
        ids += received_ids
        if total >= ordered or not may_miss_receipt:   # a receipt we could not place may hold the rest
            verdicts["in_full"] = total >= ordered
            supplier.checks["in_full"].append(Check(verdicts["in_full"], qty_ids + received_ids))
    bad = [i for k in receipt_keys for i in failed[k]]
    if bad:
        verdicts["quality"] = False
        ids += bad
    if not verdicts:
        supplier.not_judged["lines without a delivery to judge"] += 1
        return
    supplier.promises.append({**entry, **verdicts, "kept": all(verdicts.values()), "fact_ids": sorted(set(ids))})


def posterior(prior: float, k: float, kept: int, n: int) -> dict:
    alpha, beta = prior * k + kept, (1 - prior) * k + (n - kept)
    return {"alpha": alpha, "beta": beta, "mean": alpha / (alpha + beta)}


def _starting_point(h: History, has_history: bool, scenario: dict | None, k: float) -> dict:
    if scenario is not None:
        value, label, k_used = scenario.get("value"), _text(scenario.get("label")), scenario.get("k", k)
        if not label:
            raise ValueError("a scenario prior needs a label")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
            raise ValueError("a scenario prior needs a value from 0 to 1")
        if not has_history:
            return {"kind": "scenario", "value": value, "k": k_used,
                    "source": {"scenario_input": label, "value": value}}
    kept, n = h.rate()
    if not n:
        return {"kind": "unknown", "k": k}
    counts = {sid: {"kept": sum(p["kept"] for p in s.promises), "n": len(s.promises)}
              for sid, s in sorted(h.suppliers.items()) if s.promises}
    return {"kind": "all_suppliers", "value": kept / n, "k": k, "kept": kept, "n": n, "by_supplier": counts,
            "source": {"label": f"all-supplier average: {kept} of {n} promises kept", "value": kept / n}}


def card(h: History, supplier_id: str, promises=(), scenario_prior: dict | None = None,
         k: float = PRIOR_STRENGTH) -> dict:
    """The supplier record for the screen. It reads no score: only history, the ledger and a labelled prior."""
    s = h.suppliers.get(supplier_id) or SupplierHistory()
    start = _starting_point(h, bool(s.promises), scenario_prior, k)
    if not math.isfinite(start["k"]) or start["k"] <= 0:
        raise ValueError("k must be a positive number")
    score = {
        "k": start["k"],
        "starting_point": start,
        "history": {"kept": sum(p["kept"] for p in s.promises), "n": len(s.promises)},
        "inputs": deepcopy(s.promises),
        "measures": [_measure(h, s, name, k) for name in MEASURES],
        "not_judged": dict(s.not_judged),
        "outcomes": [],
    }
    if "value" in start:
        score["prior"] = start["value"]
    if scenario_prior is not None and s.promises:
        score["scenario_prior_ignored"] = {"scenario_input": scenario_prior["label"], "value": scenario_prior["value"]}
    record = {"kind": "supplier", "supplier_id": supplier_id, "score": score}
    if s.name:
        record["name"] = s.name

    latest = {p["promise_id"]: p for p in promises if p["supplier_id"] == supplier_id}
    for pid, p in sorted(latest.items()):
        if p["state"] in LEDGER_STATES:
            score["outcomes"].append({"promise_id": pid, "outcome": LEDGER_STATES[p["state"]],
                                      "sources": list(p.get("sources") or [])})
    _rescore(score)
    return record


def cards(h: History, promises=(), scenario_priors: dict | None = None, k: float = PRIOR_STRENGTH) -> list[dict]:
    promises, priors = list(promises), scenario_priors or {}
    ids = set(h.suppliers) | set(priors) | {p["supplier_id"] for p in promises}
    return [card(h, sid, promises, priors.get(sid), k) for sid in sorted(ids)]


def update(record: dict, outcome: str, promise_id: str, sources=(), at: str | None = None) -> dict:
    """The record after one promise was kept or broken. The same promise never counts twice."""
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be kept or broken, not {outcome!r}")
    new = deepcopy(record)
    score = new["score"]
    if any(o["promise_id"] == promise_id and o["outcome"] == outcome for o in score["outcomes"]):
        return new
    score["outcomes"] = [o for o in score["outcomes"] if o["promise_id"] != promise_id]
    score["outcomes"].append({"promise_id": promise_id, "outcome": outcome, "sources": list(sources)})
    _rescore(score)
    new["cause"] = f"{outcome} promise {promise_id}"
    new.pop("at", None)
    if at:
        new["at"] = at
    return new


def _measure(h: History, s: SupplierHistory, name: str, k: float) -> dict:
    label, what, where = MEASURES[name]
    checks = s.checks[name]
    kept, n = sum(c.ok for c in checks), len(checks)
    all_kept, all_n = h.rate(name)
    row = {"measure": name, "label": label, "kept": kept, "n": n, "all_suppliers": {"kept": all_kept, "n": all_n},
           "value": f"{kept} of {n} {what}" if n else "no history", "source": f"kb facts: {where}",
           "fact_ids": sorted({i for c in checks for i in c.fact_ids})}
    if all_n:
        row["posterior"] = posterior(all_kept / all_n, k, kept, n)
        row["value"] += f", {row['posterior']['mean']:.0%} after weighting with all suppliers"
    if name == "quality":
        row["inspections"] = dict(s.inspections)
        row["value"] += f" ({sum(s.inspections.values())} inspection reports)"
    return row


def _rescore(score: dict) -> None:
    past, outcomes, start = score["history"], score["outcomes"], score["starting_point"]
    ledger_kept = sum(OUTCOMES[o["outcome"]] for o in outcomes)
    kept, n = past["kept"] + ledger_kept, past["n"] + len(outcomes)
    score["n"] = n
    rows = [{"label": "Promises kept", "value": f"{past['kept']} of {past['n']} past order promises",
             "source": "kb facts: PO lines, goods receipts, inspection reports"},
            *({key: m[key] for key in ("label", "value", "source")} for m in score["measures"]),
            {"label": "This launch", "value": f"{ledger_kept} of {len(outcomes)} promises kept",
             "source": "promise ledger" + "".join(f", {o['promise_id']}" for o in outcomes)}]
    if "value" in start:
        score["posterior"] = posterior(start["value"], start["k"], kept, n)
        score["value"] = score["posterior"]["mean"]
        origin = start["source"].get("label") or f"scenario input: {start['source']['scenario_input']}"
        score["method"] = (f"{kept} of {n} promises kept, pulled toward a starting score of {start['value']:.0%} "
                           f"with the weight of {start['k']:g} promises ({origin}).")
        rows.append({"label": "Starting score", "value": f"{start['value']:.0%}", "source": origin})
        rows.append({"label": "Weight of the starting score", "value": f"{start['k']:g} promises",
                     "source": "scenario input" if start["kind"] == "scenario" else "model setting: prior strength"})
    else:
        score["method"] = "Unknown: no supplier has delivery history and no scenario prior was given."
    score["breakdown"] = rows

    sources = [{"fact_id": i} for i in sorted({i for r in score["measures"] for i in r["fact_ids"]}
                                              | {i for p in score["inputs"] for i in p["fact_ids"]})]
    if "value" in start:
        sources += [start["source"], {"label": "model setting: prior strength", "value": start["k"]}]
    for o in outcomes:
        sources += [{"label": f"promise ledger: {o['promise_id']}", "value": o["outcome"]}, *o["sources"]]
    score["sources"] = list({json.dumps(s, sort_keys=True, default=str): s for s in sources}.values())
