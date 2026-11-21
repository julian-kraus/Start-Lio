"""Fact storage: append, deactivate by document, load for evaluation."""

from __future__ import annotations

import hashlib
import json

from psycopg.types.json import Jsonb

from tracker.db import _jsonb_safe

from .engine import Fact


def fact_id(f: dict) -> str:
    raw = json.dumps([f["source_sha256"], f["fact_type"], f["subject"], f["attribute"], f.get("locator"),
                      f["extractor"]], sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _j(value):
    return None if value is None else Jsonb(_jsonb_safe(value))


def insert_facts(conn, facts: list[dict]) -> int:
    rows = []
    for f in facts:
        rows.append((
            f.get("fact_id") or fact_id(f), f["fact_type"], Jsonb(_jsonb_safe(f["subject"])), _j(f.get("subject_raw")),
            f["attribute"], Jsonb(_jsonb_safe(f.get("value"))), f.get("unit"),
            None if f.get("as_of") is None else str(f["as_of"]), _j(f.get("locator")), f.get("evidence"),
            f["extractor"], float(f.get("confidence", 1.0)), _j(f.get("checks")), f.get("note"),
            f["source_path"], f["source_sha256"], f.get("valid", True),
        ))
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO facts (fact_id, fact_type, subject, subject_raw, attribute, value, unit, as_of, locator,
                                  evidence, extractor, confidence, checks, note, source_path, source_sha256, valid)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (fact_id) DO UPDATE SET
                 value = EXCLUDED.value, as_of = EXCLUDED.as_of, evidence = EXCLUDED.evidence,
                 confidence = EXCLUDED.confidence, checks = EXCLUDED.checks, note = EXCLUDED.note,
                 valid = EXCLUDED.valid, observed_at = now()""",
            rows,
        )
    return len(rows)


def deactivate_other_versions(conn, path: str, keep_sha: str | None) -> None:
    """A path has one current version; facts of every other version stop counting (kept for history)."""
    conn.execute("UPDATE facts SET valid = false WHERE source_path = %s AND source_sha256 IS DISTINCT FROM %s AND valid",
                 (path, keep_sha))
    conn.execute("UPDATE documents SET active = false, updated_at = now() "
                 "WHERE path = %s AND sha256 IS DISTINCT FROM %s AND active", (path, keep_sha))
    conn.execute("UPDATE extraction_queue SET state = 'superseded', updated_at = now() "
                 "WHERE path = %s AND sha256 IS DISTINCT FROM %s AND state = 'pending'", (path, keep_sha))


def upsert_document(conn, path: str, sha: str, *, container: str | None, status: str, extractor: str | None = None,
                    doc_kind: str | None = None, detail: dict | None = None, active: bool = True) -> None:
    conn.execute(
        """INSERT INTO documents (path, sha256, container, doc_kind, status, extractor, detail, active)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (path, sha256) DO UPDATE SET
             container = coalesce(EXCLUDED.container, documents.container),
             doc_kind = coalesce(EXCLUDED.doc_kind, documents.doc_kind),
             status = EXCLUDED.status, extractor = coalesce(EXCLUDED.extractor, documents.extractor),
             detail = EXCLUDED.detail, active = EXCLUDED.active, updated_at = now()""",
        (path, sha, container, doc_kind, status, extractor, Jsonb(_jsonb_safe(detail or {})), active),
    )


def has_valid_facts(conn, sha: str) -> bool:
    return conn.execute("SELECT EXISTS (SELECT 1 FROM facts WHERE source_sha256 = %s AND valid) AS e",
                        (sha,)).fetchone()["e"]


def load_valid_facts(conn, proposed_cap: float = 0.5) -> list[Fact]:
    """Valid facts as the rules see them: fact types renamed through promoted aliases, and confidence capped for facts
    whose doc kind or fact type is still only proposed (so they show up but cannot win against settled sources)."""
    rows = conn.execute(
        """SELECT f.fact_id, coalesce(ft.alias_of, f.fact_type) AS fact_type, f.subject, f.attribute, f.value,
                  f.as_of, f.extractor, f.source_path, f.source_sha256, f.evidence, f.unit, d.doc_kind,
                  CASE WHEN dk.status = 'proposed' OR (ft.status = 'proposed' AND ft.alias_of IS NULL)
                       THEN least(f.confidence, %s) ELSE f.confidence END AS confidence
           FROM facts f
           LEFT JOIN documents d ON d.path = f.source_path AND d.sha256 = f.source_sha256
           LEFT JOIN doc_kinds dk ON dk.name = d.doc_kind
           LEFT JOIN fact_types ft ON ft.name = f.fact_type
           WHERE f.valid AND coalesce(ft.status, 'core') <> 'rejected'""", (proposed_cap,)
    ).fetchall()
    return [Fact(**r) for r in rows]
