"""Load the Codex output in structured/ (contract, schema maps, envelopes) into the kb schema.

Everything is optional and idempotent: it can run while Codex is still writing, and again when more output exists.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from psycopg.types.json import Jsonb

from tracker.db import _jsonb_safe

from . import events, registry
from . import facts as facts_mod
from .tabular import TABULAR_CONTAINERS

log = logging.getLogger(__name__)
DEFAULT_CONFIDENCE = {"high": 0.9, "medium": 0.75, "low": 0.4}


def load_contract(conn, structured: Path, origin: str = "codex") -> dict:
    counts = {"fact_types": 0, "doc_kinds": 0, "schema_maps": 0, "schema_map_errors": []}
    contract = structured / "contract"
    ft_path = contract / "fact_types.json"
    if ft_path.is_file():
        data = json.loads(ft_path.read_text())
        items = data.get("fact_types", data) if isinstance(data, dict) else data
        if isinstance(items, dict):
            items = [{"name": k, **(v if isinstance(v, dict) else {"description": str(v)})} for k, v in items.items()]
        for item in items:
            name = item.get("name") or item.get("fact_type")
            if not name:
                continue
            conn.execute(
                """INSERT INTO fact_types (name, description, attributes, origin) VALUES (%s, %s, %s, %s)
                   ON CONFLICT (name) DO UPDATE SET description = EXCLUDED.description,
                     attributes = EXCLUDED.attributes, origin = EXCLUDED.origin""",
                (name, item.get("description"), Jsonb(_jsonb_safe(item.get("attributes"))), origin))
            counts["fact_types"] += 1
    for md in sorted((contract / "doc_kinds").glob("*.md")):
        registry.upsert_doc_kind(conn, md.stem, md.read_text(), origin=origin, status="core")
        counts["doc_kinds"] += 1
    for path in sorted((structured / "schema_maps").glob("*.json")):
        try:
            mapping = json.loads(path.read_text())
            if not mapping.get("signature") or not mapping.get("fact_type") or "columns" not in mapping:
                raise ValueError("needs signature, fact_type and columns")
            if not all(isinstance(s, dict) and "field" in s for s in mapping["columns"].values()):
                raise ValueError("every column must map to an object with a 'field'")
        except ValueError as exc:
            counts["schema_map_errors"].append(f"{path.name}: {exc}")
            continue
        registry.upsert_schema_map(conn, mapping, origin=origin, status="core")
        counts["schema_maps"] += 1
    conn.commit()
    return counts


def current_sha_by_path(conn, data_dir: Path) -> dict[str, str]:
    """The tracker's view of what is current; falls back to hashing data/current if the tracker table is absent."""
    if conn.execute("SELECT to_regclass('public.file_versions') IS NOT NULL AS ok").fetchone()["ok"]:
        return {r["path"]: r["sha256"] for r in conn.execute(
            "SELECT path, sha256 FROM public.file_versions WHERE superseded_at IS NULL")}
    out = {}
    root = data_dir / "current"
    for p in root.rglob("*"):
        if p.is_file():
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def envelope_facts(env: dict, confidence_map: dict) -> list[dict]:
    src = env["source"]
    by = env.get("extracted_by") or "codex"
    if by.startswith("llm:"):
        extractor = by                       # live intake: the adapter label
    else:
        extractor = f"{by}:{env.get('spec_version', '0')}"
        if not extractor.startswith("codex"):
            extractor = "codex:" + extractor  # first pass: Codex worker and spec version
    out = []
    for f in env.get("facts", []):
        if not f.get("fact_type") or not f.get("attribute"):
            continue
        conf = f.get("confidence", "medium")
        out.append({
            "fact_type": f["fact_type"], "subject": f.get("subject") or {}, "subject_raw": f.get("subject_raw"),
            "attribute": f["attribute"], "value": f.get("value"), "unit": f.get("unit"), "as_of": f.get("as_of"),
            "locator": f.get("locator"), "evidence": f.get("evidence"), "note": f.get("note"),
            "extractor": extractor,
            "confidence": float(conf) if isinstance(conf, (int, float)) else confidence_map.get(conf, 0.5),
            "source_path": src["path"], "source_sha256": src["sha256"],
        })
    return out


def load_envelope(conn, env: dict, *, current_sha: str | None, confidence_map: dict, emit: bool = True) -> int:
    """Insert one envelope. Tabular files parsed by kb itself keep kb's facts for the sheets kb could parse."""
    src = env["source"]
    path, sha = src["path"], src["sha256"]
    is_current = current_sha == sha
    before = events.snapshot(conn, path)
    items = envelope_facts(env, confidence_map)
    parsed_sheets = {r["sheet"] for r in conn.execute(
        "SELECT DISTINCT locator->>'sheet' AS sheet FROM facts WHERE source_sha256 = %s AND extractor = 'kb:tabular' "
        "AND valid", (sha,))}
    if parsed_sheets:
        items = [f for f in items if (f.get("locator") or {}).get("sheet") not in parsed_sheets]
    for f in items:
        f["fact_id"] = facts_mod.fact_id(f)
    if is_current and items and {f["fact_id"] for f in items} == before["fact_ids"]:
        stored = {r["fact_id"]: r["value"] for r in conn.execute(
            "SELECT fact_id, value FROM facts WHERE source_path = %s AND valid", (path,))}
        if all(stored.get(f["fact_id"]) == f.get("value") for f in items):
            return len(items)  # this exact extraction (ids and values) is already loaded and the only valid one
    # A newer extraction of the same file version replaces the previous one of the same extractor family
    # (a regenerated Codex pass, or a re-run of the live intake); facts it no longer contains stop counting.
    family = (items[0]["extractor"] if items else envelope_facts({**env, "facts": [
        {"fact_type": "x", "attribute": "x"}]}, confidence_map)[0]["extractor"]).split(":")[0] + ":%"
    conn.execute("UPDATE facts SET valid = false WHERE source_path = %s AND source_sha256 = %s AND extractor LIKE %s "
                 "AND valid", (path, sha, family))
    for f in items:
        f["valid"] = is_current
    facts_mod.insert_facts(conn, items)
    status = conn.execute("SELECT status FROM documents WHERE path = %s AND sha256 = %s", (path, sha)).fetchone()
    new_status = "parsed" if status and status["status"] == "parsed" else "loaded"
    facts_mod.upsert_document(conn, path, sha, container=None, status=new_status,
                              extractor=items[0]["extractor"] if items else "codex", doc_kind=env.get("doc_kind"),
                              detail={"unreadable": env.get("unreadable", []),
                                      "proposed_fact_types": env.get("proposed_fact_types", [])},
                              active=is_current)
    if emit:
        events.emit(conn, path, before, sha=sha, status=new_status)
    if is_current:
        conn.execute("UPDATE extraction_queue SET state = 'done', updated_at = now() "
                     "WHERE path = %s AND sha256 = %s AND state = 'pending'", (path, sha))
    for proposal in env.get("proposed_fact_types", []) or []:
        name = proposal.get("name") if isinstance(proposal, dict) else None
        if name:
            conn.execute("INSERT INTO fact_types (name, description, attributes, origin, status) "
                         "VALUES (%s, %s, %s, %s, 'proposed') ON CONFLICT (name) DO NOTHING",
                         (name, proposal.get("description"), Jsonb(_jsonb_safe(proposal.get("attributes"))),
                          env.get("extracted_by") or "proposed"))
    return len(items)


def read_envelope(path: Path) -> dict | None:
    try:
        env = json.loads(path.read_text())
    except ValueError as exc:
        log.warning("skipping %s: %s", path, exc)
        return None
    src = env.get("source") or {}
    if not src.get("path") or not src.get("sha256") or not isinstance(env.get("facts", []), list):
        log.warning("skipping %s: missing source.path, source.sha256 or facts", path)
        return None
    return env


def load_envelopes(conn, structured: Path, data_dir: Path, confidence_map: dict | None = None) -> dict:
    confidence_map = confidence_map or DEFAULT_CONFIDENCE
    current = current_sha_by_path(conn, data_dir)
    counts = {"envelopes": 0, "facts": 0, "stale_envelopes": 0, "invalid": 0}
    for path in sorted((structured / "docs").glob("*.json")):
        env = read_envelope(path)
        if env is None:
            counts["invalid"] += 1
            continue
        cur = current.get(env["source"]["path"])
        if cur != env["source"]["sha256"]:
            counts["stale_envelopes"] += 1
        counts["facts"] += load_envelope(conn, env, current_sha=cur, confidence_map=confidence_map)
        counts["envelopes"] += 1
        conn.commit()
    return counts


def envelope_for(structured: Path, sha: str) -> dict | None:
    path = structured / "docs" / f"{sha}.json"
    return read_envelope(path) if path.is_file() else None


def is_tabular(container: str | None) -> bool:
    return container in TABULAR_CONTAINERS
