"""Validation gate for LLM output: nothing an adapter returns becomes a valid fact without passing these checks."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from jsonschema import validators

ROW_ID = re.compile(r"^(row|line|r)[-_ ]?\d+$", re.IGNORECASE)
_SEP = re.compile(r"[\s  ,.'’]")


@dataclass
class GateResult:
    fatal: list[str] = field(default_factory=list)       # envelope rejected: facts stored as not valid
    warnings: list[str] = field(default_factory=list)
    ungrounded: list[int] = field(default_factory=list)  # indexes of facts whose numbers are not in the text layer

    @property
    def ok(self) -> bool:
        return not self.fatal


def load_schema(structured: Path) -> dict | None:
    path = structured / "contract" / "envelope.schema.json"
    return json.loads(path.read_text()) if path.is_file() else None


def _digits(text: str) -> str:
    return _SEP.sub("", text)


def _numbers(value) -> list[str]:
    if isinstance(value, bool) or value is None:
        return []
    if isinstance(value, (int, float)):
        v = int(value) if float(value).is_integer() else value
        return [str(v)] if abs(float(value)) >= 100 else []
    return []


def check(env: dict, *, schema: dict | None, known_types: set[str], text: str | None,
          kind_checks: list[dict] | None = None, max_ungrounded_share: float = 0.2) -> GateResult:
    res = GateResult()
    if schema:
        validator = validators.validator_for(schema)(schema)
        errors = sorted(validator.iter_errors(env), key=lambda e: list(e.path))
        res.fatal += [f"schema: {'/'.join(map(str, e.path)) or '(root)'}: {e.message[:200]}" for e in errors[:10]]
    facts = env.get("facts") or []
    for i, f in enumerate(facts):
        subj = f.get("subject") or {}
        if any(ROW_ID.match(str(v)) for v in subj.values()):
            res.fatal.append(f"fact {i}: subject uses a row number instead of an entity ID: {subj}")
            break
    unknown = sorted({f.get("fact_type") for f in facts
                      if f.get("fact_type") not in known_types and not str(f.get("fact_type")).startswith("x_")})
    if known_types and unknown:
        res.fatal.append(f"fact types outside the contract (use x_<name> for new information): {unknown}")
    if text and len(text.strip()) > 200:
        haystack = _digits(text)
        for i, f in enumerate(facts):
            for n in _numbers(f.get("value")):
                if _digits(n) not in haystack:
                    res.ungrounded.append(i)
        numeric = sum(1 for f in facts if _numbers(f.get("value")))
        if numeric and len(res.ungrounded) / numeric > max_ungrounded_share:
            res.fatal.append(f"{len(res.ungrounded)} of {numeric} numbers do not occur in the text layer")
        elif res.ungrounded:
            res.warnings.append(f"{len(res.ungrounded)} numbers not found in the text layer (confidence lowered)")
    for rule in kind_checks or []:
        res.warnings += _record_check(facts, rule)
    return res


def _record_check(facts: list[dict], rule: dict) -> list[str]:
    """Doc-kind check of the form {fact_type: x, sum: [a, b], equals: c}: per record, a + b == c."""
    out = []
    records: dict[str, dict] = {}
    for f in facts:
        if f.get("fact_type") != rule.get("fact_type"):
            continue
        records.setdefault(json.dumps(f.get("subject"), sort_keys=True), {})[f.get("attribute")] = f.get("value")
    for key, attrs in records.items():
        parts = [attrs.get(a) for a in rule.get("sum", [])]
        total = attrs.get(rule.get("equals"))
        if total is None or any(p is None for p in parts):
            continue
        try:
            if abs(sum(float(p) for p in parts) - float(total)) > 1e-6:
                out.append(f"check failed for {key}: {' + '.join(rule['sum'])} != {rule['equals']}")
        except (TypeError, ValueError):
            out.append(f"check not numeric for {key}")
    return out


def apply(env: dict, res: GateResult, low_confidence: str = "low") -> dict:
    """Mark ungrounded facts and attach the gate result to the envelope."""
    for i in res.ungrounded:
        f = env["facts"][i]
        f["confidence"] = low_confidence
        f["note"] = ((f.get("note") or "") + " [number not found in the text layer]").strip()
    env["gate"] = {"fatal": res.fatal, "warnings": res.warnings, "ungrounded": len(res.ungrounded)}
    return env
