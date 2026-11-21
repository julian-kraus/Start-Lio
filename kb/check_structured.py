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
    seen: Counter = Counter()
    for path in sorted((structured / "docs").glob("*.json")):
        env = json.loads(path.read_text())
        src = env.get("source", {}).get("path", path.name)
        for f in env.get("facts", []):
            ft, subj = f.get("fact_type"), f.get("subject") or {}
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
    return {"ok": not failures, "failures": failures, "maps": maps, "envelopes": env}
