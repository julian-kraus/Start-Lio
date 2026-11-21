"""Resolver: repairs joins between records before the rules run. Pure and configurable through rule-set settings.

1. Line matching (`line_match`): a record that points at a line of another table (a confirmation item at a PO line)
   is re-keyed when the line it names does not fit and exactly one line of the same document does, judged by the
   match attributes (e.g. material and quantity). Confirmations that print the same item number for different PO
   lines are the motivating case.
2. Part references in text (`text_parts`): when the text of a record names exactly one known part (a Soony part ID
   or a supplier part number from the cross-reference facts), that part becomes the record's material. A remark that
   names its part is better evidence than an extractor's guess.

Every change is reported in the diagnostics, so the UI can show why a record moved.
"""

from __future__ import annotations

import re
from collections import defaultdict

from .engine import Record, normalise

_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-./]*[A-Za-z0-9]")


def _same(a, b) -> bool:
    na, nb = normalise(a, "number"), normalise(b, "number")
    if na is not None and nb is not None:
        return abs(na - nb) < 1e-9
    return normalise(a, "text") == normalise(b, "text")


def _rekey(rec: Record, field: str, value) -> Record:
    rec.subject = {**rec.subject, field: value}
    return rec


def match_lines(records: list[Record], spec: dict) -> list[dict]:
    """spec: {source: oc_line, target: po_line, document_key: po_number, line_key: po_line,
              match: [material_id, qty]}"""
    src, tgt = spec["source"], spec["target"]
    doc_key, line_key, attrs = spec["document_key"], spec["line_key"], list(spec["match"])
    lines: dict[str, list[Record]] = defaultdict(list)
    for r in records:
        if r.fact_type == tgt and r.get(doc_key) is not None:
            lines[str(r.get(doc_key))].append(r)
    changes = []
    for r in records:
        if r.fact_type != src or r.get(doc_key) is None:
            continue
        candidates = lines.get(str(r.get(doc_key)), [])
        if not candidates:
            continue

        def fits(line: Record, used: list[str]) -> bool:
            return all(r.get(a) is not None and line.get(a) is not None and _same(r.get(a), line.get(a))
                       for a in used)

        current = [c for c in candidates if _same(c.get(line_key), r.get(line_key))]
        if current and fits(current[0], attrs):
            continue
        for used in (attrs, attrs[:1]):        # all match attributes first, then the first one alone
            hits = {str(c.get(line_key)): c for c in candidates if fits(c, used)}
            if len(hits) == 1:
                new = next(iter(hits.values())).get(line_key)
                changes.append({"fact_type": src, "subject": dict(r.subject), "field": line_key, "to": new,
                                "matched_on": used, "source_path": r.source_path})
                _rekey(r, line_key, new)
                break
    return changes


def text_parts(records: list[Record], spec: dict) -> list[dict]:
    """spec: {fact_types: [supplier_statement], text_attributes: [value], material_key: material_id,
              xref: {fact_type: part_xref, part_attribute: supplier_part_no, material_attribute: material_id},
              id_fact_type: component}"""
    xref = spec.get("xref") or {}
    aliases: dict[str, set] = defaultdict(set)
    for r in records:
        if r.fact_type == xref.get("fact_type"):
            part, mat = r.get(xref.get("part_attribute")), r.get(xref.get("material_attribute"))
            if part and mat:
                aliases[str(part).strip()].add(str(mat))
        if r.fact_type == spec.get("id_fact_type"):
            mat = r.get(spec.get("material_key", "material_id"))
            if mat:
                aliases[str(mat).strip()].add(str(mat))
    key = spec.get("material_key", "material_id")
    changes = []
    for r in records:
        if r.fact_type not in spec.get("fact_types", []):
            continue
        text = " ".join(str(r.get(a) or "") for a in spec.get("text_attributes", ["value"]))
        found = set()
        for token in _TOKEN.findall(text):
            found |= aliases.get(token, set())
        if len(found) != 1:
            continue
        material = next(iter(found))
        if r.subject.get(key) != material:
            changes.append({"fact_type": r.fact_type, "subject": dict(r.subject), "field": key, "to": material,
                            "source_path": r.source_path, "reason": "named in the text"})
            _rekey(r, key, material)
    return changes


def resolve(records: list[Record], settings: dict) -> list[dict]:
    changes = []
    for spec in settings.get("line_match") or []:
        changes += match_lines(records, spec)
    for spec in settings.get("text_parts") or []:
        changes += text_parts(records, spec)
    return changes
