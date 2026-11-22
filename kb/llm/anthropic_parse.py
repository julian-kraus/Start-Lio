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
# A per-unit ratio (qty_per, qty_per_pallet ...) carries a "per" segment. It is a quantity attribute but not a
# count: "1.250" there is 1.25 per unit, not 1250, so a single thousands group must not be read into it.
_RATIO_ATTR = re.compile(r"(?:^|_)per(?:_|$)")
# Text that is meant as a number but did not parse cleanly: digits with separators only.
_NUMERIC_LOOKING = re.compile(r"[+-]?[\d\s   .,']*\d[\d\s   .,']*")
# Subject keys whose reference values are integers (line positions and statement numbers). The contract lists
# subject key names but not their types, so the type is fixed here by name; every other key stays text, which
# keeps leading zeros in PO numbers, material ids and the like.
_INT_SUBJECT = re.compile(r"(?:^|_)line$|^statement_no$")
# Order-confirmation lines print the PO item number (10, 20, 30 ...), while po_line in the records is the ERP line
# index, the printed item number divided by ten. A model copies the printed number, so po_line is corrected in code
# when it equals the record's own item_no_printed.
_PRINTED_ITEM_ATTR, _PRINTED_ITEM_STEP = "item_no_printed", 10
# The contract types dates and flags as string, so they are recognised by attribute name. Records hold ISO dates
# and true/false flags; a model copies "20.11.2026" or "yes" as printed.
_DATE_ATTR = re.compile(r"(?:^|_)date$")
_FLAG_ATTR = re.compile(r"(?:_present$|^struck_through$|^is_|^has_)")
_ISO = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})")
_DMY = re.compile(r"(\d{1,2})([./])(\d{1,2})\2(\d{4})")
_DATE_IN_TEXT = re.compile(r"(?<!\d)(?:\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}([./])\d{1,2}\1\d{4})(?!\d)")
# a measured value printed with its unit ("1 250 kg", "12 pcs"): number in value, unit in unit
_FRACTION = re.compile(r"[+-]?0[.,]\d+")       # 0.125 is a fraction, never 125
_MEASURE_ATTR = re.compile(r"(?:^|_)(?:weight|qty|quantity|packages)(?:_|$)")
_WITH_UNIT = re.compile(r"\s*([+-]?\d[\d\s.,']*?)\s*([A-Za-z]{1,5}\.?)\s*")
# languages whose slash dates put the day first; with English in the document a slash date stays ambiguous
_DAY_FIRST = {"de", "fr", "it", "es", "nl", "pl", "pt", "cs", "da", "sv", "nb", "no", "fi", "hu", "ro", "tr"}
_YES, _NO = {"yes", "y", "true", "ja", "x", "1", "present", "signed"}, {"no", "n", "false", "nein", "0", "absent", "none"}
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
    stripped = s.lstrip("+-")
    # A thousands group never starts with a zero group, so a value such as "0.125" or "0 750" is a fraction or
    # plain text, never the grouped integer 125 or 750. Only the digit-grouping readings below are blocked; a
    # leading-zero decimal falls through to the decimal reading unchanged.
    leading_zero = stripped[:1] == "0"
    if not leading_zero:
        if _SPACE_GROUPED.fullmatch(s):
            # Whitespace is read only as well-formed thousands groups, so the separators are safe to drop.
            return int(_GROUP_SPACES.sub("", s))
        if _GROUPED.fullmatch(s):
            return int(s.replace(".", "").replace(",", ""))
        if count and _SINGLE_GROUP.fullmatch(s):
            return int(s.replace(".", "").replace(",", ""))
    if _DEC.fullmatch(s):
        # A dot followed by exactly three digits is ambiguous between a decimal and a single thousands group, so
        # outside a count (and only when there is no leading zero) keep it as text rather than guess a number.
        if len(stripped.split(".")[1]) == 3 and not leading_zero:
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


def _iso_date(value: str, day_first: bool = False) -> str | None:
    """ISO date for an unambiguous printed date: year first, a dotted day-month-year, or a slash date whose first
    part cannot be a month. Anything else (a slash date that could be either order) stays as printed."""
    s = value.strip()
    if (m := _ISO.fullmatch(s)):
        y, mo, d = m.groups()
    elif (m := _DMY.fullmatch(s)):
        d, sep, mo, y = m.groups()
        if sep == "/" and int(d) <= 12 and not day_first:
            return None
    else:
        return None
    if not (1 <= int(mo) <= 12 and 1 <= int(d) <= 31):
        return None
    return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"


def _typed_by_name(f: dict, day_first: bool = False) -> None:
    """Dates to ISO and flags to booleans by attribute name, keeping the printed text as evidence. A date attribute
    left null whose evidence quotes exactly one readable date takes that date."""
    value, attr = f.get("value"), str(f.get("attribute") or "")
    if value is None and _DATE_ATTR.search(attr) and isinstance(f.get("evidence"), str):
        found = {d for m in _DATE_IN_TEXT.finditer(f["evidence"]) if (d := _iso_date(m.group(0), day_first))}
        if len(found) == 1:
            f["value"] = found.pop()
        return
    if not isinstance(value, str):
        return
    new = None
    if _DATE_ATTR.search(attr):
        new = _iso_date(value, day_first)
    elif _FLAG_ATTR.search(attr):
        low = value.strip().lower()
        new = True if low in _YES else False if low in _NO else None
    elif _MEASURE_ATTR.search(attr) and (m := _WITH_UNIT.fullmatch(value)):
        digits = m.group(1).strip()
        if _FRACTION.fullmatch(digits):      # 0.125 / 0,500: a fraction below one, never thousands
            number = float(digits.replace(",", "."))
        else:
            number = _coerce_number(digits, count=bool(_QUANTITY_ATTR.search(attr)))
        if number is not None:
            if not f.get("unit"):
                f["unit"] = m.group(2).rstrip(".")
            new = number
    if new is not None and new != value:
        if f.get("evidence") is None:
            f["evidence"] = value
        f["value"] = new


def _po_line_from_printed_item(facts: list[dict]) -> None:
    """po_line = printed item number // 10, but only for a confirmation that numbers its lines 10, 20, 30 ...
    (every printed item a multiple of the step). A document numbered 1, 2, 3 ... prints its own line index, so it
    is left alone; and a mapped line is never written onto a line that is already present, so no line is lost to a
    collision (item 10 would otherwise become line 1 and overwrite line 1)."""
    printed = {}                                        # subject key -> printed item number
    for f in facts:
        if f.get("attribute") == _PRINTED_ITEM_ATTR and isinstance(f.get("subject"), dict):
            key = json.dumps(f["subject"], sort_keys=True, default=str)
            digits = str(f.get("value") or "").strip()
            if _INT.fullmatch(digits):
                printed[key] = int(digits)
    if not printed or any(item % _PRINTED_ITEM_STEP for item in printed.values()):
        return                                          # not the tens convention: the printed number is the line
    # A record is remapped only where its po_line still equals its printed item (the model copied it in).
    remapped = {key: item for key, item in printed.items()}
    kept_lines = {subj.get("po_line") for f in facts
                  if isinstance((subj := f.get("subject")), dict) and isinstance(subj.get("po_line"), int)
                  and remapped.get(json.dumps(subj, sort_keys=True, default=str)) != subj["po_line"]}
    for f in facts:
        subject = f.get("subject")
        if not isinstance(subject, dict) or not isinstance(subject.get("po_line"), int):
            continue
        item = remapped.get(json.dumps(subject, sort_keys=True, default=str))
        if item != subject["po_line"]:
            continue
        new_line = item // _PRINTED_ITEM_STEP
        if new_line in kept_lines:                      # a line already present: do not overwrite it
            continue
        f["subject"] = {**subject, "po_line": new_line}


def _positions(facts: list[dict]) -> None:
    """A positional subject key (line, *_line, statement_no) that the model filled with a code instead of a number is
    renumbered 1, 2, 3 ... in order of first appearance within its record group (same fact type and other keys).
    Groups that mix numbers and codes are left alone: there is no safe mapping."""
    groups: dict[str, dict] = {}
    for f in facts:
        subject = f.get("subject")
        if not isinstance(subject, dict):
            continue
        for k in subject:
            if _INT_SUBJECT.search(str(k)):
                rest = {kk: vv for kk, vv in subject.items() if kk != k}
                gkey = json.dumps([f.get("fact_type"), k, rest], sort_keys=True, default=str)
                g = groups.setdefault(gkey, {"key": k, "values": [], "facts": []})
                if subject[k] not in g["values"]:
                    g["values"].append(subject[k])
                g["facts"].append(f)
    for g in groups.values():
        values = g["values"]
        if not values or any(isinstance(v, int) or v is None for v in values):
            continue
        number = {v: i for i, v in enumerate(values, start=1)}
        for f in g["facts"]:
            f["subject"] = {**f["subject"], g["key"]: number[f["subject"][g["key"]]]}


def _identifiers_as_attributes(facts: list[dict], attrs_by_type: dict) -> None:
    """When the contract lists an attribute under the same name as a subject key (supplier_id, route_id, ...), the
    record carries that value in its subject; state it as the attribute too if the model did not. Copied, not read:
    the fact cites the subject and keeps the confidence of the record's other facts."""
    records: dict[str, dict] = {}
    for f in facts:
        subject = f.get("subject")
        if isinstance(subject, dict) and f.get("fact_type") in attrs_by_type:
            key = json.dumps([f["fact_type"], subject], sort_keys=True, default=str)
            records.setdefault(key, {"first": f, "attrs": set()})["attrs"].add(f.get("attribute"))
    for rec in records.values():
        first = rec["first"]
        for k, v in first["subject"].items():
            if k in attrs_by_type[first["fact_type"]] and k not in rec["attrs"] and v not in (None, ""):
                facts.append({"fact_type": first["fact_type"], "subject": dict(first["subject"]), "attribute": k,
                              "value": v, "unit": None, "locator": first.get("locator"),
                              "evidence": f"identifier of the record ({k})", "confidence": first.get("confidence")})


def _envelope(data: dict, default_kind: str, numeric_attrs: dict | None = None,
              attrs_by_type: dict | None = None) -> dict:
    numeric_attrs = numeric_attrs or {}
    facts, ambiguous = [], []
    langs = {str(x).lower().split("-")[0] for x in (data.get("languages") or []) if isinstance(x, str)}
    day_first = bool(langs) and "en" not in langs and langs <= _DAY_FIRST
    for f in data.get("facts") or []:
        if not isinstance(f, dict):
            continue
        f = dict(f)
        if isinstance(f.get("subject"), dict):
            f["subject"] = _coerce_subject(f["subject"])
        _typed_by_name(f, day_first)
        value, attr = f.get("value"), f.get("attribute")
        if isinstance(value, str) and attr in numeric_attrs.get(f.get("fact_type"), ()):
            count = bool(_QUANTITY_ATTR.search(str(attr))) and not _RATIO_ATTR.search(str(attr))
            number = _coerce_number(value, count=count)
            if number is not None:
                if f.get("evidence") is None:            # keep the verbatim text the number was read from
                    f["evidence"] = value
                f["value"] = number
            elif count and _NUMERIC_LOOKING.fullmatch(value.strip()):
                # A count whose text is not one clean number is never emitted as a usable fact: left in facts it
                # would pass the gate as text and kb.engine.normalise("21 3", "number") would read 213. Drop it
                # and record it in unreadable so the gap is visible, never a wrong number.
                ambiguous.append(f"{f.get('fact_type')}.{attr}: quantity text could not be read as one number; "
                                 f"not loaded as a fact")
                continue
        facts.append(f)
    _po_line_from_printed_item(facts)
    _positions(facts)
    _identifiers_as_attributes(facts, attrs_by_type or {})
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
