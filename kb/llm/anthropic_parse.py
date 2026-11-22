"""Turn a model answer into an envelope the pipeline can use: JSON parsing, shape checks, number parsing for
quantity attributes, and subject values coerced to the types the reference records use.

Split out of anthropic.py so the transport, cache and prompts stay in one file and the answer handling in this one.
Every example number below is synthetic.
"""

from __future__ import annotations

import json
import re

from tracker.config import PROJECT_ROOT

SPEC_VERSION = "anthropic-extract-v1"
_SP = "[ \t   ]"         # the space characters a thousands group may use, not newlines
_GROUP_SPACES = re.compile(r"[\s   ]")
_INT = re.compile(r"[+-]?\d+")
_DEC = re.compile(r"[+-]?\d+\.\d+")
# Two or more same-separator groups of three ("9.876.543", "9,876,543") cannot be a decimal, so it is safe to
# read them as one integer. A single group ("98.765") is ambiguous with a decimal outside a count attribute.
_GROUPED = re.compile(r"[+-]?\d{1,3}([.,])\d{3}(?:\1\d{3})+")
# One group of three after a dot or comma. In a count attribute this is thousands ("98.765" pieces), because a
# count has no fractional part; elsewhere it stays ambiguous.
_SINGLE_GROUP = re.compile(r"[+-]?\d{1,3}[.,]\d{3}")
# Space-grouped thousands: a leading group of one to three digits, then one or more groups of exactly three.
# Only this shape is read across whitespace, so "98 765" is one number but "7 654 98 765" (a middle group of
# two digits), "21 3" (a trailing group of one) and anything split across a newline stay text: two numbers are
# never silently joined.
_SPACE_GROUPED = re.compile(rf"[+-]?\d{{1,3}}(?:{_SP}\d{{3}})+")
# Attributes whose name marks a quantity (qty, quantity, final_qty, printed_qty, qty_ordered ...). The contract
# types some of these as string (handwritten or printed scans), yet their values are counts the gate must
# ground, so they are parsed by name as well as by a declared `type: number`.
_QUANTITY_ATTR = re.compile(r"(?:^|_)(?:qty|quantity)(?:_|$)")
# Text that is meant as a number but did not parse cleanly: digits with separators only.
_NUMERIC_LOOKING = re.compile(r"[+-]?[\d\s   .,']*\d[\d\s   .,']*")
# Subject keys whose reference values are integers (line positions and statement numbers). The contract lists
# subject key names but not their types, so the type is fixed here by name; every other key stays text, which
# keeps leading zeros in PO numbers, material ids and the like.
_INT_SUBJECT = re.compile(r"(?:^|_)line$|^statement_no$")
_ENVELOPE_KEYS = ("source", "doc_kind", "doc_kind_confidence", "languages", "spec_version", "extracted_by",
                  "doc_date", "facts", "unreadable", "proposed_fact_types")
# Subject keys per fact type come from this read-only contract file (contract_summary omits them).
_FT_CONTRACT = PROJECT_ROOT / "structured" / "contract" / "fact_types.json"


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("the model did not return a JSON object")
        data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("the model returned JSON that is not an object")
    return data


def _coerce_number(value: str, count: bool = False):
    """Parse a verbatim number, or None when it is not clean. `count` is for quantity attributes, where a single
    group of three after a dot or comma is thousands."""
    s = value.strip()
    if _INT.fullmatch(s):
        return int(s)
    if _SPACE_GROUPED.fullmatch(s):
        # Whitespace is read only as well-formed thousands groups, so the separators are safe to drop.
        return int(_GROUP_SPACES.sub("", s))
    if _GROUPED.fullmatch(s):
        return int(s.replace(".", "").replace(",", ""))
    if count and _SINGLE_GROUP.fullmatch(s):
        return int(s.replace(".", "").replace(",", ""))
    if _DEC.fullmatch(s):
        # Outside a count, a dot followed by exactly three digits is ambiguous between a decimal and a single
        # thousands group, so keep it as text rather than guess a wrong number.
        if len(s.lstrip("+-").split(".")[1]) == 3:
            return None
        return float(s)
    return None


def _declared_subject_keys() -> dict:
    """Subject keys per fact type from the canonical contract file, cached; without them the model invents its
    own subject key and no fact matches the reference. A missing file degrades to no hint, not an error."""
    cache = _declared_subject_keys.__dict__.setdefault("cache", {})
    if not cache:
        try:
            for t in json.loads(_FT_CONTRACT.read_text()).get("fact_types", []):
                if t.get("name") and t.get("subject_keys"):
                    cache[t["name"]] = list(t["subject_keys"])
        except (OSError, ValueError):
            cache["_"] = []     # mark attempted so a missing file is not re-read per document
    return cache


def _numeric_attrs(contract: dict) -> dict:
    """Map each contract fact type to the attributes whose value is parsed into a number: every attribute the
    contract declares `type: number`, plus every attribute whose name marks a quantity. Everything else
    (identifiers, dates, codes) is kept verbatim, so a value like "007" keeps its leading zero."""
    out: dict[str, set] = {}
    for t in contract.get("fact_types") or []:
        attrs = t.get("attributes") or {}
        out[t.get("name")] = {a for a, spec in attrs.items()
                              if (isinstance(spec, dict) and spec.get("type") == "number")
                              or _QUANTITY_ATTR.search(a)}
    return out


def _check_shape(role: str, data: dict) -> None:
    """Reject an answer whose shape the pipeline cannot use, before it is cached, so a broken answer is never
    stored and replayed and each retry is a fresh call. An extract answer must carry facts as a list of objects,
    each with an object subject and a string fact_type and attribute."""
    if role != "extract":
        return
    facts = data.get("facts")
    if not isinstance(facts, list):
        raise ValueError("extract answer has no facts list")
    for f in facts:
        if not isinstance(f, dict):
            raise ValueError("a fact is not an object")
        if not isinstance(f.get("subject"), dict):
            raise ValueError("a fact subject is not an object of entity ids")
        if not isinstance(f.get("fact_type"), str) or not isinstance(f.get("attribute"), str):
            raise ValueError("a fact has a fact_type or attribute that is not a string")


def _coerce_subject(subject: dict) -> dict:
    """Integer-typed subject keys (line positions, statement numbers) as int when the model wrote digits as text,
    so a fact keyed {"line": "3"} matches a record keyed {"line": 3}."""
    out = {}
    for k, v in subject.items():
        if isinstance(v, str) and _INT_SUBJECT.search(str(k)) and _INT.fullmatch(v.strip()):
            v = int(v.strip())
        out[k] = v
    return out


def _envelope(data: dict, default_kind: str, numeric_attrs: dict | None = None) -> dict:
    numeric_attrs = numeric_attrs or {}
    facts, ambiguous = [], []
    for f in data.get("facts") or []:
        if not isinstance(f, dict):
            continue
        f = dict(f)
        if isinstance(f.get("subject"), dict):
            f["subject"] = _coerce_subject(f["subject"])
        value, attr = f.get("value"), f.get("attribute")
        if isinstance(value, str) and attr in numeric_attrs.get(f.get("fact_type"), ()):
            count = bool(_QUANTITY_ATTR.search(str(attr)))
            number = _coerce_number(value, count=count)
            if number is not None:
                if f.get("evidence") is None:            # keep the verbatim text the number was read from
                    f["evidence"] = value
                f["value"] = number
            elif count and _NUMERIC_LOOKING.fullmatch(value.strip()):
                # a count that does not parse cleanly is not passed on as text: mark it for review
                f["confidence"] = "low"
                f["note"] = ((f.get("note") or "") + " [ambiguous quantity, needs review]").strip()
                ambiguous.append(f"{f.get('fact_type')}.{attr}: quantity text could not be read as one number")
        facts.append(f)
    unreadable = data.get("unreadable") if isinstance(data.get("unreadable"), list) else []
    env = {
        "source": {},
        "doc_kind": data.get("doc_kind") or default_kind,
        "doc_kind_confidence": data.get("doc_kind_confidence") or "medium",
        "languages": data.get("languages") if isinstance(data.get("languages"), list) else [],
        "spec_version": SPEC_VERSION,
        "extracted_by": "",
        "doc_date": data.get("doc_date"),
        "facts": facts,
        "unreadable": list(unreadable) + ambiguous,
        "proposed_fact_types": (data.get("proposed_fact_types")
                                if isinstance(data.get("proposed_fact_types"), list) else []),
    }
    return {k: env[k] for k in _ENVELOPE_KEYS}
