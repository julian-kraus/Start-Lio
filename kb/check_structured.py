"""Acceptance check for structured/ (the Codex output). No database needed.

It checks what the pipeline relies on, not what the data should say:
- every table in data/current has a schema map that kb can apply, and the map parses without errors;
- envelopes use entity IDs as subjects (never row numbers), one record per entity, and the contract's vocabulary;
- per fact type, how many of the contract's attributes are actually filled.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from tracker.diff import detect_kind

from .tabular import TABULAR_CONTAINERS, find_header, guess_header, read_tables, signature, table_to_facts

ROW_ID = re.compile(r"^(row|line|r)[-_ ]?\d+$", re.IGNORECASE)
STATEMENT_KINDS = {"on_hold", "term_rejected", "short_shipment", "artwork_rev", "approval_claim", "dispatch_info",
                   "price_deviation", "handling_note", "other"}
ARITHMETIC = [("shipment_qty", ("qty_shipped", "qty_balance"), "qty_planned")]
MAP_KEYS = ("signature", "fact_type", "subject", "columns")


def check_maps(structured: Path, current: Path) -> dict:
    maps, problems = {}, []
    for path in sorted((structured / "schema_maps").glob("*.json")):
        try:
            m = json.loads(path.read_text())
        except ValueError as exc:
            problems.append(f"{path.name}: invalid JSON ({exc})")
            continue
        missing = [k for k in MAP_KEYS if not m.get(k)]
        bad_cols = [c for c, spec in (m.get("columns") or {}).items()
                    if not isinstance(spec, dict) or "field" not in spec]
        if missing:
            problems.append(f"{path.name}: missing {missing}")
        if bad_cols:
            problems.append(f"{path.name}: columns must map 'header as written' -> {{field, type, ...}}; "
                            f"not an object for {bad_cols[:5]}")
        if not missing and not bad_cols:
            maps[m["signature"]] = m | {"_file": path.name}
    tables, unmatched, parse = 0, [], []
    for f in sorted(current.rglob("*")):
        if not f.is_file():
            continue
        data = f.read_bytes()
        rel = str(f.relative_to(current))
        kind = detect_kind(rel, data)
        if kind not in TABULAR_CONTAINERS:
            continue
        for t in read_tables(kind, data):
            if not t.rows:
                continue
            tables += 1
            hit = find_header(t, maps, kind)
            if hit is None:
                header = guess_header(t)
                unmatched.append({"path": rel, "sheet": t.sheet, "header_guess": header[:12],
                                  "signature_if_that_is_the_header": signature(header, t.sheet if kind == "xlsx" else None)})
                continue
            idx, m = hit
            facts, stats = table_to_facts(t, idx, m, path=rel, sha256="check")
            subjects_bad = sum(1 for x in facts if any(ROW_ID.match(str(v)) for v in x["subject"].values()))
            parse.append({"path": rel, "sheet": t.sheet, "map": m["_file"], "fact_type": m["fact_type"],
                          "rows": stats["rows"], "skipped_rows": stats["skipped_rows"],
                          "parse_errors": stats["parse_errors"], "error_samples": stats["error_samples"][:3],
                          "row_id_subjects": subjects_bad})
    return {"maps_valid": len(maps), "map_problems": problems, "tables": tables, "unmatched_tables": unmatched,
            "parsed": parse}


def check_envelopes(structured: Path) -> dict:
    contract = structured / "contract" / "fact_types.json"
    registry = {}
    if contract.is_file():
        data = json.loads(contract.read_text())
        items = data.get("fact_types", data) if isinstance(data, dict) else data
        if isinstance(items, dict):
            items = [{"name": k, **v} for k, v in items.items()]
        registry = {i["name"]: i for i in items if i.get("name")}
    row_subjects, collisions, unknown = Counter(), Counter(), Counter()
    attr_fill: dict[str, Counter] = defaultdict(Counter)
    records: dict[str, set] = defaultdict(set)
    statements_without_kind = 0
    stmt_kinds: dict[tuple, set] = defaultdict(set)
    missing_keys, file_name_ids, bad_kinds, unanchored = Counter(), Counter(), Counter(), Counter()
    by_record: dict[tuple, dict] = defaultdict(dict)
    seen: Counter = Counter()
    for path in sorted((structured / "docs").glob("*.json")):
        env = json.loads(path.read_text())
        src = env.get("source", {}).get("path", path.name)
        stem = Path(src).stem
        for f in env.get("facts", []):
            ft, subj = f.get("fact_type"), f.get("subject") or {}
            declared = (registry.get(ft) or {}).get("subject_keys") or []
            if any(k not in subj for k in declared):
                missing_keys[ft] += 1
            if any(str(v) == stem for v in subj.values()):
                file_name_ids[ft] += 1
            if ft == "supplier_statement" and not (set(subj) - {"statement_no", "material_id"}):
                unanchored[src] += 1
            if ft == "supplier_statement" and f.get("attribute") == "kind" and f.get("value") not in STATEMENT_KINDS:
                bad_kinds[str(f.get("value"))] += 1
            by_record[(src, ft, json.dumps(subj, sort_keys=True))][f.get("attribute")] = f.get("value")
            if any(ROW_ID.match(str(v)) for v in subj.values()):
                row_subjects[ft] += 1
            if ft not in registry and not str(ft).startswith("x_"):
                unknown[ft] += 1
            key = (src, ft, json.dumps(subj, sort_keys=True), f.get("attribute"))
            seen[key] += 1
            if seen[key] == 2:
                collisions[ft] += 1
            rkey = json.dumps([src, subj], sort_keys=True)
            records[ft].add(rkey)
            attr_fill[ft][f.get("attribute")] += 1
            if ft == "supplier_statement":
                stmt_kinds[(src, json.dumps(subj, sort_keys=True))].add(f.get("attribute"))
    statements_without_kind = sum(1 for attrs in stmt_kinds.values() if "kind" not in attrs)
    # Cross-check: a confirmation item and the PO line with the same PO number and quantity name the same material.
    po_lines = defaultdict(list)
    for (src, ft, subj), attrs in by_record.items():
        if ft == "po_line":
            s = json.loads(subj)
            po_lines[str(s.get("po_number"))].append(attrs)
    material_mismatches = []
    for (src, ft, subj), attrs in by_record.items():
        if ft != "oc_line" or attrs.get("material_id") is None or attrs.get("qty") is None:
            continue
        s = json.loads(subj)
        same_qty = [p for p in po_lines.get(str(s.get("po_number")), []) if str(p.get("qty")) == str(attrs["qty"])]
        if len(same_qty) == 1 and same_qty[0].get("material_id") != attrs["material_id"]:
            material_mismatches.append(f"{src}: item {attrs.get('supplier_part_no')} qty {attrs['qty']} -> "
                                       f"{attrs['material_id']}, PO line with that qty is {same_qty[0].get('material_id')}")
    arithmetic = []
    for (src, ft, subj), attrs in by_record.items():
        for afft, parts, total in ARITHMETIC:
            if ft != afft or attrs.get(total) is None or any(attrs.get(p) is None for p in parts):
                continue
            try:
                if abs(sum(float(attrs[p]) for p in parts) - float(attrs[total])) > 1e-6:
                    arithmetic.append(f"{src}: {' + '.join(parts)} != {total} for {subj}")
            except (TypeError, ValueError):
                arithmetic.append(f"{src}: non-numeric {parts}/{total} for {subj}")
    coverage = {}
    for ft, spec in registry.items():
        attrs = list((spec.get("attributes") or {}).keys())
        n = len(records.get(ft, ()))
        if not n or not attrs:
            continue
        coverage[ft] = {"records": n, "attributes": {a: round(attr_fill[ft][a] / n, 2) for a in attrs}}
    return {"row_number_subjects": dict(row_subjects),
            "subject_collisions": dict(collisions),
            "unknown_fact_types": dict(unknown),
            "supplier_statements_without_kind": statements_without_kind,
            "subjects_missing_declared_keys": dict(missing_keys),
            "file_name_used_as_id": dict(file_name_ids),
            "statement_kinds_outside_list": dict(bad_kinds),
            "statements_without_document_key": len(unanchored),
            "arithmetic_failures": arithmetic[:20],
            "oc_material_vs_po": material_mismatches[:20],
            "oc_material_vs_po_count": len(material_mismatches),
            "attribute_coverage": coverage}


def run(structured: Path, current: Path) -> dict:
    maps = check_maps(structured, current)
    env = check_envelopes(structured)
    failures = []
    if maps["map_problems"]:
        failures.append(f"{len(maps['map_problems'])} schema maps do not follow the format")
    if maps["unmatched_tables"]:
        failures.append(f"{len(maps['unmatched_tables'])} tables have no applicable schema map")
    if any(p["parse_errors"] for p in maps["parsed"]):
        failures.append("schema maps produce parse errors")
    if any(p["row_id_subjects"] for p in maps["parsed"]) or env["row_number_subjects"]:
        failures.append("subjects use row numbers instead of entity IDs")
    if env["subject_collisions"]:
        failures.append("several facts share fact type, subject and attribute in one document (records collapse)")
    if env["supplier_statements_without_kind"]:
        failures.append("supplier statements without a kind")
    if env["unknown_fact_types"]:
        failures.append("fact types outside the contract")
    if env["subjects_missing_declared_keys"]:
        failures.append("subjects miss keys that fact_types.json declares (records cannot be joined)")
    if env["file_name_used_as_id"]:
        failures.append("a file name is used as an entity ID")
    if env["statements_without_document_key"]:
        failures.append("supplier statements not linked to a PO, delivery note or shipment (subject is only a number)")
    if env["statement_kinds_outside_list"]:
        failures.append("supplier statement kinds outside the closed list")
    if env["oc_material_vs_po_count"]:
        failures.append("confirmation items name a different material than the PO line with the same quantity "
                        "(check part-number cross-references)")
    if env["arithmetic_failures"]:
        failures.append("quantities that must add up do not (e.g. shipped + balance = ordered)")
    return {"ok": not failures, "failures": failures, "maps": maps, "envelopes": env}
