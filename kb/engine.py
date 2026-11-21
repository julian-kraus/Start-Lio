"""Pure rule evaluation: facts -> records -> believed values and contradictions.

No database access here, so the same code runs for real runs, previews of draft rule sets and tests.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from fnmatch import fnmatchcase

SEVERITY_ORDER = {"high": 0, "cost": 1, "medium": 2, "low": 3, "info": 4}
_NUMBER_CLEAN = re.compile(r"[\s  ]")
_TEXT_CLEAN = re.compile(r"[\s_\-]+")


@dataclass
class Fact:
    fact_id: str
    fact_type: str
    subject: dict
    attribute: str
    value: object
    as_of: str | None
    confidence: float
    extractor: str
    source_path: str
    source_sha256: str
    evidence: str | None = None
    unit: str | None = None
    doc_kind: str | None = None


@dataclass
class Record:
    """All facts of one entity from one source document (e.g. one CSV row, one OC item)."""
    fact_type: str
    subject: dict
    source_path: str
    source_sha256: str
    extractor: str
    doc_kind: str | None
    attrs: dict[str, Fact] = field(default_factory=dict)

    def get(self, name: str):
        if name in self.subject:
            return self.subject[name]
        fact = self.attrs.get(name)
        return fact.value if fact else None


@dataclass
class Side:
    label: str
    candidate_index: int
    value: object
    facts: list[Fact]

    @property
    def confidence(self) -> float:
        return min(f.confidence for f in self.facts)

    @property
    def as_of(self) -> str:
        return max((f.as_of or "") for f in self.facts)


@dataclass
class Believed:
    rule_id: str
    key: dict
    value: object
    winner_label: str | None
    winning_fact_ids: list[str]
    losing_fact_ids: list[str]
    contradiction: bool
    explanation: str
    sides: list[Side]


@dataclass
class Issue:
    issue_key: str
    kind: str
    severity: str
    title: str
    payload: dict
    fact_ids: list[str]


def issue_key(kind: str, scope: str, key: dict | str) -> str:
    raw = json.dumps([kind, scope, key], sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def build_records(facts: list[Fact]) -> list[Record]:
    records: dict[tuple, Record] = {}
    for f in facts:
        k = (f.source_sha256, f.fact_type, json.dumps(f.subject, sort_keys=True, default=str), f.extractor)
        rec = records.get(k)
        if rec is None:
            rec = records[k] = Record(f.fact_type, f.subject, f.source_path, f.source_sha256, f.extractor, f.doc_kind)
        current = rec.attrs.get(f.attribute)
        if current is None or (f.as_of or "", f.confidence) > (current.as_of or "", current.confidence):
            rec.attrs[f.attribute] = f
    return list(records.values())


def derive_change_records(records: list[Record], change_type: str, now: datetime,
                           subject_exclude: tuple[str, ...] = ("field", "change_field", "change_id")
                           ) -> tuple[list[Record], list[dict]]:
    """Turn effective change records (field, new, effective_from) into derived facts on the changed entity.

    The fact type of the derived fact is taken from `target_fact_type` if the change record names one, otherwise
    from the non-derived records of the same subject that carry the changed attribute. Nothing is assumed about
    which fields can change."""
    by_subject: dict[str, set[str]] = {}
    for r in records:
        if r.fact_type != change_type:
            for attr in r.attrs:
                by_subject.setdefault(_subject_key(r.subject), set()).add(f"{r.fact_type}\x00{attr}")
    today = now.date().isoformat()
    derived: list[Record] = []
    skipped: list[dict] = []
    for r in records:
        if r.fact_type != change_type:
            continue
        field_name = r.get("field") or r.get("change_field")
        new = r.get("new")
        target_subject = {k: v for k, v in r.subject.items() if k not in subject_exclude}
        if field_name in (None, "") or new is None:
            continue
        effective = r.get("effective_from")
        base = r.attrs.get("new")
        if base is None:
            continue
        as_of = str(effective) if effective else (base.as_of if base else None)
        if as_of and as_of[:10] > today:
            skipped.append({"subject": r.subject, "field": field_name, "reason": f"effective {as_of[:10]}"})
            continue
        target = r.get("target_fact_type")
        if not target:
            matches = sorted(t.split("\x00")[0] for t in by_subject.get(_subject_key(target_subject), ())
                             if t.split("\x00")[1] == field_name)
            if len(matches) != 1:
                skipped.append({"subject": r.subject, "field": field_name,
                                "reason": "no unique fact type carries this field" if not matches else
                                f"ambiguous target types {matches}"})
                continue
            target = matches[0]
        fact = Fact(
            fact_id=f"derived:{base.fact_id}", fact_type=target, subject=target_subject, attribute=str(field_name),
            value=new, as_of=as_of, confidence=base.confidence, extractor=f"derived:{r.extractor}",
            source_path=r.source_path, source_sha256=r.source_sha256, evidence=base.evidence, doc_kind=r.doc_kind,
        )
        rec = Record(target, target_subject, r.source_path, r.source_sha256, fact.extractor, r.doc_kind,
                     {fact.attribute: fact})
        derived.append(rec)
    return derived, skipped


def _subject_key(subject: dict) -> str:
    return json.dumps(subject, sort_keys=True, default=str)


def normalise(value, mode: str):
    if value is None:
        return None
    if mode == "number":
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        text = _NUMBER_CLEAN.sub("", str(value))
        try:
            return float(text)
        except ValueError:
            return None
    if mode == "date":
        return str(value).strip()[:10]
    return _TEXT_CLEAN.sub(" ", str(value)).strip().casefold()


def _matches(rec: Record, cand: dict) -> bool:
    if rec.fact_type != cand["fact_type"] or cand["attribute"] not in rec.attrs:
        return False
    if "extractor" in cand and not fnmatchcase(rec.extractor, cand["extractor"]):
        return False
    if "extractor_not" in cand and fnmatchcase(rec.extractor, cand["extractor_not"]):
        return False
    if "source_path" in cand and not fnmatchcase(rec.source_path, cand["source_path"]):
        return False
    if "doc_kind" in cand and rec.doc_kind != cand["doc_kind"]:
        return False
    for attr, expected in (cand.get("where") or {}).items():
        if normalise(rec.get(attr), "text") != normalise(expected, "text"):
            return False
    return True


def _key_of(rec: Record, key_fields: list[str], key_map: dict) -> tuple | None:
    out = []
    for name in key_fields:
        value = rec.get(key_map.get(name, name))
        if value in (None, ""):
            return None
        out.append(str(value).strip())
    return tuple(out)


def _candidate_value(rec: Record, cand: dict):
    """Returns (value, keep). A value_map entry mapping to null drops the record from this rule."""
    raw = rec.attrs[cand["attribute"]].value
    vmap = cand.get("value_map")
    if vmap:
        norm = {normalise(k, "text"): v for k, v in vmap.items()}
        k = normalise(raw, "text")
        if k in norm:
            return norm[k], norm[k] is not None
    return raw, True


def _equal(a, b, mode: str, rule: dict) -> bool:
    na, nb = normalise(a, mode), normalise(b, mode)
    if mode == "number" and na is not None and nb is not None:
        tol = float(rule.get("tolerance", 0))
        pct = float(rule.get("tolerance_pct", 0))
        return abs(na - nb) <= max(tol, abs(nb) * pct / 100)
    return na == nb


def _format(template: str, **values) -> str:
    class Safe(dict):
        def __missing__(self, key):
            return "{" + key + "}"
    return template.format_map(Safe(values))


def _display(value) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{value:,}"
    return str(value)


def evaluate_rule(rule: dict, records: list[Record]) -> list[Believed]:
    mode = rule.get("compare", "text")
    key_fields = list(rule["key"])
    groups: dict[tuple, dict[int, list[tuple[Record, object]]]] = {}
    for idx, cand in enumerate(rule["candidates"]):
        for rec in records:
            if not _matches(rec, cand):
                continue
            key = _key_of(rec, key_fields, cand.get("key_map") or {})
            if key is None:
                continue
            value, keep = _candidate_value(rec, cand)
            if keep:
                groups.setdefault(key, {}).setdefault(idx, []).append((rec, value))

    results = []
    for key, by_cand in groups.items():
        sides = []
        for idx in sorted(by_cand):
            cand = rule["candidates"][idx]
            items = by_cand[idx]
            facts = [rec.attrs[cand["attribute"]] for rec, _ in items]
            if cand.get("aggregate") == "sum":
                nums = [normalise(v, "number") for _, v in items]
                value = sum(n for n in nums if n is not None)
            else:
                rec, value = max(items, key=lambda it: (it[0].attrs[cand["attribute"]].as_of or "",
                                                        it[0].attrs[cand["attribute"]].confidence))
                facts = [rec.attrs[cand["attribute"]]]
            sides.append(Side(cand.get("label", cand["fact_type"]), idx, value, facts))

        contradiction = any(not _equal(s.value, sides[0].value, mode, rule) for s in sides[1:])
        winner = _decide(rule, sides, mode)
        others = [s for s in sides if s is not winner]
        key_dict = dict(zip(key_fields, key))
        if winner is None:
            explanation = _format(rule.get("explanation", ""), winner="nobody", winner_value="undecided",
                                  others="; ".join(f"{s.label}: {_display(s.value)}" for s in sides))
        else:
            explanation = _format(rule.get("explanation", ""), winner=winner.label,
                                  winner_value=_display(winner.value),
                                  others="; ".join(f"{s.label}: {_display(s.value)}" for s in others) or "no other source")
        results.append(Believed(
            rule_id=rule["rule_id"], key=key_dict, value=None if winner is None else winner.value,
            winner_label=None if winner is None else winner.label,
            winning_fact_ids=[] if winner is None else [f.fact_id for f in winner.facts],
            losing_fact_ids=[f.fact_id for s in others for f in s.facts],
            contradiction=contradiction, explanation=explanation, sides=sides,
        ))
    return results


def _decide(rule: dict, sides: list[Side], mode: str) -> Side | None:
    decide = rule.get("decide", "preference")
    if decide == "none":
        return None if len(sides) > 1 else sides[0]
    if decide == "newest":
        return max(sides, key=lambda s: (s.as_of, -s.candidate_index))
    min_conf = float(rule.get("min_confidence", 0))
    for side in sides:
        if side.confidence >= min_conf:
            return side
    fallback = rule.get("fallback", "none")
    numeric = [s for s in sides if normalise(s.value, "number") is not None]
    if fallback in ("lowest", "highest") and numeric:
        pick = min if fallback == "lowest" else max
        return pick(numeric, key=lambda s: normalise(s.value, "number"))
    return None


def contradiction_issues(rule: dict, believed: list[Believed], max_sides: int = 6) -> list[Issue]:
    issues = []
    for b in believed:
        if not b.contradiction:
            continue
        key_text = ", ".join(f"{k} {v}" for k, v in b.key.items())
        sides_text = " vs ".join(f"{s.label} {_display(s.value)}" for s in b.sides[:max_sides])
        payload = {
            "rule_id": rule["rule_id"],
            "question": rule.get("question"),
            "key": b.key,
            "sides": [{
                "label": s.label, "value": s.value, "confidence": s.confidence,
                "facts": [{"fact_id": f.fact_id, "source_path": f.source_path, "evidence": f.evidence,
                           "extractor": f.extractor, "as_of": f.as_of} for f in s.facts[:20]],
            } for s in b.sides[:max_sides]],
            "believed": b.value,
            "believed_from": b.winner_label,
            "needs_decision": b.winner_label is None,
            "explanation": b.explanation,
        }
        issues.append(Issue(
            issue_key=issue_key("contradiction", rule["rule_id"], b.key),
            kind="contradiction",
            severity=rule.get("severity", "medium"),
            title=f"{key_text}: {sides_text}",
            payload=payload,
            fact_ids=b.winning_fact_ids + b.losing_fact_ids,
        ))
    return issues


def evaluate(rules: list[dict], records: list[Record], settings: dict, now: datetime):
    """Returns (believed values, issues, diagnostics)."""
    derived, skipped = derive_change_records(
        records, settings.get("change_record_fact_type", "change_record"), now,
        tuple(settings.get("change_record_subject_exclude", ("field", "change_field", "change_id"))))
    from .resolve import resolve
    resolved = resolve(records, settings)
    all_records = records + derived
    believed: list[Believed] = []
    issues: list[Issue] = []
    for rule in rules:
        res = evaluate_rule(rule, all_records)
        believed.extend(res)
        issues.extend(contradiction_issues(rule, res, int(settings.get("max_issue_sides", 6))))
    diagnostics = {"records": len(records), "derived": len(derived), "derived_skipped": skipped[:50],
                   "resolved": len(resolved), "resolved_samples": resolved[:50]}
    return believed, issues, diagnostics
