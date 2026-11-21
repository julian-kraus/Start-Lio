"""Survey: route a file with cheap, explainable checks before any LLM is involved.

Order: (1) same content already processed under any path -> reuse its facts; (2) container format;
(3) tables -> header signature (handled in ingest); (4) documents -> recognition rules of the doc kinds.
Only when (4) finds no kind or several kinds does the LLM survey (`classify`) get the file.
"""

from __future__ import annotations

import hashlib
from fnmatch import fnmatchcase

from psycopg.types.json import Jsonb

from .llm.base import Routing, Structure


def reuse_facts(conn, path: str, sha: str) -> int:
    """Copy the valid facts of identical content seen under another path. Returns the number of facts copied."""
    other = conn.execute(
        "SELECT source_path FROM facts WHERE source_sha256 = %s AND source_path <> %s AND valid LIMIT 1",
        (sha, path)).fetchone()
    if other is None:
        return 0
    rows = conn.execute(
        """SELECT fact_id, fact_type, subject, subject_raw, attribute, value, unit, as_of, locator, evidence, extractor,
                  confidence, checks, note FROM facts WHERE source_sha256 = %s AND source_path = %s AND valid""",
        (sha, other["source_path"])).fetchall()
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO facts (fact_id, fact_type, subject, subject_raw, attribute, value, unit, as_of, locator,
                                  evidence, extractor, confidence, checks, note, source_path, source_sha256, valid)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true)
               ON CONFLICT (fact_id) DO UPDATE SET valid = true""",
            [(hashlib.sha256(f"{r['fact_id']}|{path}".encode()).hexdigest()[:32], r["fact_type"], Jsonb(r["subject"]),
              Jsonb(r["subject_raw"]) if r["subject_raw"] is not None else None, r["attribute"], Jsonb(r["value"]),
              r["unit"], r["as_of"], Jsonb(r["locator"]) if r["locator"] is not None else None, r["evidence"],
              r["extractor"], r["confidence"],
              Jsonb({**(r["checks"] or {}), "reused_from": other["source_path"]}), r["note"], path, sha)
             for r in rows])
    return len(rows)


def match_kinds(path: str, container: str, text: str | None, kinds: list[Structure]) -> list[tuple[str, str]]:
    """Doc kinds whose recognition rules fit. A rule set matches when every rule it states holds:
    container equal, any path glob matches, any phrase occurs (case-insensitive) in the text."""
    hits = []
    folded = (text or "").casefold()
    for kind in kinds:
        rec = kind.recognise or {}
        if not rec:
            continue
        reasons = []
        if rec.get("container"):
            wanted = rec["container"] if isinstance(rec["container"], list) else [rec["container"]]
            if container not in wanted:
                continue
            reasons.append(f"container {container}")
        globs = rec.get("path_globs") or []
        if globs:
            glob = next((g for g in globs if fnmatchcase(path, g)), None)
            if glob is None:
                continue
            reasons.append(f"path matches {glob}")
        phrases = rec.get("any_phrases") or []
        if phrases:
            phrase = next((p for p in phrases if p.casefold() in folded), None)
            if phrase is None:
                continue
            reasons.append(f"text contains {phrase!r}")
        if reasons:
            hits.append((kind.name, ", ".join(reasons)))
    return hits


def route_document(path: str, container: str, text: str | None, kinds: list[Structure]) -> Routing:
    hits = match_kinds(path, container, text, kinds)
    if len(hits) == 1:
        return Routing("kind", hits[0][0], hits[0][1], "rules")
    if not hits:
        return Routing("none", None, "no recognition rule matches", "rules")
    # Prefer approved/core kinds over proposed ones when both match.
    status = {k.name: k.status for k in kinds}
    settled = [h for h in hits if status.get(h[0]) in ("core", "approved")]
    if len(settled) == 1:
        return Routing("kind", settled[0][0], settled[0][1] + " (proposed kinds also matched)", "rules")
    return Routing("ambiguous", None, "several kinds match: " + "; ".join(f"{n} ({r})" for n, r in hits), "rules")
