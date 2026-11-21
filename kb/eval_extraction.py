"""Score an LLM adapter against the reference envelopes (Codex's verified first pass), per doc kind and field.

A reference fact is matched on (fact type, subject, attribute). Matched facts agree when their values are equal after
normalisation (numbers as numbers, dates to the day, text case- and whitespace-insensitive). Facts the adapter
produces that the reference does not have are counted as extra, not as errors: the reference may be incomplete.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from psycopg.types.json import Jsonb

from tracker.db import _jsonb_safe
from tracker.diff import detect_kind

from . import registry
from .engine import normalise
from .llm.base import LLMClient
from .llm.base import normalise as prepare
from .loader import read_envelope


def _key(f: dict) -> str:
    return json.dumps([f.get("fact_type"), f.get("subject") or {}, f.get("attribute")], sort_keys=True, default=str)


def _same(a, b) -> bool:
    na, nb = normalise(a, "number"), normalise(b, "number")
    if na is not None and nb is not None:
        return abs(na - nb) < 1e-9
    if isinstance(a, list) or isinstance(b, list):
        return sorted(map(str, a if isinstance(a, list) else [a])) == sorted(map(str, b if isinstance(b, list) else [b]))
    return normalise(a, "text") == normalise(b, "text")


def compare(reference: dict, candidate: dict) -> dict:
    ref = {_key(f): f for f in reference.get("facts", [])}
    cand = {_key(f): f for f in candidate.get("facts", [])}
    fields: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])   # attribute -> [ref, matched, agree]
    disagreements = []
    for k, f in ref.items():
        attr = f"{f.get('fact_type')}.{f.get('attribute')}"
        fields[attr][0] += 1
        if k in cand:
            fields[attr][1] += 1
            if _same(f.get("value"), cand[k].get("value")):
                fields[attr][2] += 1
            elif len(disagreements) < 20:
                disagreements.append({"fact": attr, "subject": f.get("subject"), "reference": f.get("value"),
                                      "candidate": cand[k].get("value")})
    return {"ref_facts": len(ref), "matched": sum(v[1] for v in fields.values()),
            "agree": sum(v[2] for v in fields.values()), "extra_facts": len(set(cand) - set(ref)),
            "fields": {a: {"ref": v[0], "matched": v[1], "agree": v[2]} for a, v in fields.items()},
            "disagreements": disagreements}


def run(conn, client: LLMClient, structured: Path, read_bytes, current: dict[str, str], *,
        kinds: list[str] | None = None, limit: int = 20, store: bool = True) -> dict:
    schema_path = structured / "contract" / "envelope.schema.json"
    schema = json.loads(schema_path.read_text()) if schema_path.is_file() else {}
    contract = registry.contract_summary(conn)
    per_kind: dict[str, dict] = {}
    for path in sorted((structured / "docs").glob("*.json")):
        ref = read_envelope(path)
        if ref is None:
            continue
        kind = ref.get("doc_kind") or "unknown"
        if kinds and kind not in kinds:
            continue
        src = ref["source"]
        if current.get(src["path"]) != src["sha256"]:
            continue          # only score against files that are still current
        bucket = per_kind.setdefault(kind, {"files": 0, "ref_facts": 0, "matched": 0, "agree": 0, "extra_facts": 0,
                                            "errors": [], "disagreements": []})
        if bucket["files"] >= limit:
            continue
        structure = registry.structure(conn, kind)
        if structure is None:
            bucket["errors"].append(f"{src['path']}: doc kind {kind!r} not in the registry")
            continue
        try:
            data = read_bytes(src["sha256"], src["path"])
            doc = prepare(src["path"], src["sha256"], detect_kind(src["path"], data), data,
                          render=client.needs_images)
            cand = client.extract(doc, structure, schema, contract)
        except Exception as exc:
            bucket["errors"].append(f"{src['path']}: {type(exc).__name__}: {exc}"[:300])
            continue
        result = compare(ref, cand)
        bucket["files"] += 1
        for k in ("ref_facts", "matched", "agree", "extra_facts"):
            bucket[k] += result[k]
        bucket["disagreements"] += [{"path": src["path"], **d} for d in result["disagreements"][:5]]
    for kind, b in per_kind.items():
        b["agreement"] = round(b["agree"] / b["ref_facts"], 3) if b["ref_facts"] else None
        if store and b["files"]:
            conn.execute(
                """INSERT INTO extraction_evals (adapter, doc_kind, files, ref_facts, matched, agree, extra_facts,
                                                 agreement, detail) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (client.label, kind, b["files"], b["ref_facts"], b["matched"], b["agree"], b["extra_facts"],
                 b["agreement"], Jsonb(_jsonb_safe({"errors": b["errors"][:20],
                                                    "disagreements": b["disagreements"][:30]}))))
    if store:
        conn.commit()
    return per_kind
