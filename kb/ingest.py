"""Turn one file version into facts: tables through schema maps, documents through existing envelopes or the
extraction queue. Idempotent per (path, sha256), so at-least-once events and re-syncs are safe."""

from __future__ import annotations

import logging
from pathlib import Path

import psycopg

from tracker.diff import detect_kind
from tracker.store import BlobStore

from psycopg.types.json import Jsonb

from . import designer, events, registry, survey
from . import facts as facts_mod
from .llm.base import text_of
from .loader import DEFAULT_CONFIDENCE, envelope_for, load_envelope
from .registry import ACTIVE_MAP_STATUS
from .tabular import TABULAR_CONTAINERS, find_header, guess_header, read_tables, table_to_facts

log = logging.getLogger(__name__)
MAIL_PREFIX = "mail/"   # supplier email (kb.mail_source): never in the tracker's file set


def is_mail(path: str | None) -> bool:
    return bool(path) and path.startswith(MAIL_PREFIX)


class FileReader:
    def __init__(self, data_dir: Path):
        self.store = BlobStore(data_dir)
        self.current = data_dir / "current"

    def __call__(self, sha: str, path: str) -> bytes:
        if self.store.has(sha):
            return self.store.read(sha)
        local = self.current / path
        if local.is_file():
            return local.read_bytes()
        raise FileNotFoundError(f"no blob {sha} and no current file for {path}")


def schema_maps(conn) -> dict[str, dict]:
    return {r["signature"]: r["mapping"] for r in conn.execute(
        "SELECT signature, mapping FROM schema_maps WHERE status = ANY(%s)", (list(ACTIVE_MAP_STATUS),))}


def process_file(conn, path: str, sha: str, data: bytes, structured: Path,
                 confidence_map: dict | None = None, llm=None) -> str:
    """Returns the document status. Emits a fact event when the path's valid facts changed."""
    before = events.snapshot(conn, path)
    status = _process_file(conn, path, sha, data, structured, confidence_map, llm)
    events.emit(conn, path, before, sha=sha, status=status)
    return status


def _has_own_facts(conn, path: str, sha: str) -> bool:
    return conn.execute("SELECT EXISTS (SELECT 1 FROM facts WHERE source_path = %s AND source_sha256 = %s AND valid) "
                        "AS e", (path, sha)).fetchone()["e"]


def _process_file(conn, path: str, sha: str, data: bytes, structured: Path,
                  confidence_map: dict | None = None, llm=None) -> str:
    confidence_map = confidence_map or DEFAULT_CONFIDENCE
    container = detect_kind(path, data)
    facts_mod.deactivate_other_versions(conn, path, sha)
    status, detail = "loaded", {}

    if container in TABULAR_CONTAINERS:
        maps = schema_maps(conn)
        try:
            tables = read_tables(container, data)
        except Exception as exc:
            tables, detail = [], {"error": f"{type(exc).__name__}: {exc}"}
        parsed, unmatched, stats, designed = [], [], {}, []
        for table in tables:
            if not table.rows:
                continue
            hit = find_header(table, maps, container)
            if hit is None and llm is not None and llm.supports("design"):
                outcome = designer.propose_table_map(conn, llm, path, sha, container, table)
                designed.append(outcome)
                if outcome["status"] == "auto":
                    maps = schema_maps(conn)
                    hit = find_header(table, maps, container)
            if hit is None:
                unmatched.append({"sheet": table.sheet, "header_guess": guess_header(table)[:40]})
                continue
            header_index, mapping = hit
            items, s = table_to_facts(table, header_index, mapping, path=path, sha256=sha)
            facts_mod.insert_facts(conn, items)
            parsed.append(table.sheet)
            stats[table.sheet or "table"] = {"fact_type": mapping["fact_type"], **s}
        if parsed:
            # kb's own parse wins over an envelope for the same sheets; other sheets keep envelope facts.
            # An email with the same bytes is its sender's statement and keeps its facts.
            if container == "xlsx":
                conn.execute("UPDATE facts SET valid = false WHERE source_sha256 = %s AND extractor <> 'kb:tabular' "
                             "AND NOT starts_with(source_path, %s) AND locator->>'sheet' = ANY(%s)",
                             (sha, MAIL_PREFIX, parsed))
            else:
                conn.execute("UPDATE facts SET valid = false WHERE source_sha256 = %s AND extractor <> 'kb:tabular' "
                             "AND NOT starts_with(source_path, %s)", (sha, MAIL_PREFIX))
        env = envelope_for(structured, sha)
        if env is not None:
            load_envelope(conn, env, current_sha=sha, confidence_map=confidence_map, emit=False)
        covered_by_envelope = env is not None and any(f.get("fact_type") for f in env.get("facts", []))
        if detail.get("error") and not covered_by_envelope:
            status = "failed"
        elif unmatched and not covered_by_envelope:
            status = "partial" if parsed else "needs_schema_map"
        else:
            status = "parsed" if parsed else "loaded"
        detail = {**detail, "parsed": stats, "unmatched": unmatched, "designed": designed}
        facts_mod.upsert_document(conn, path, sha, container=container, status=status,
                                  extractor="kb:tabular" if parsed else None, detail=detail)
        return status

    doc_kind = None
    if _has_own_facts(conn, path, sha):
        status = "loaded"
    elif survey.reuse_facts(conn, path, sha):
        status, detail = "reused", {"routing": {"decision": "reuse", "by": "hash", "reason": "same content seen"}}
    else:
        env = envelope_for(structured, sha)
        if env is not None:
            load_envelope(conn, env, current_sha=sha, confidence_map=confidence_map, emit=False)
            status, doc_kind = "loaded", env.get("doc_kind")
        else:
            text, _ = text_of(container, data)
            routing = survey.route_document(path, container, text, registry.structures(conn))
            doc_kind = routing.kind
            detail = {"routing": vars(routing)}
            conn.execute(
                """INSERT INTO extraction_queue (path, sha256, container, doc_kind, routing) VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (path, sha256) DO UPDATE SET doc_kind = EXCLUDED.doc_kind, routing = EXCLUDED.routing,
                     state = CASE WHEN extraction_queue.state = 'superseded' THEN 'pending'
                                  ELSE extraction_queue.state END, updated_at = now()""",
                (path, sha, container, doc_kind, Jsonb(vars(routing))))
            status = "queued"
    existing = conn.execute("SELECT status FROM documents WHERE path = %s AND sha256 = %s", (path, sha)).fetchone()
    facts_mod.upsert_document(conn, path, sha, container=container, doc_kind=doc_kind,
                              status=existing["status"] if existing and status == "loaded" else status, detail=detail)
    return status


def remove_path(conn, path: str) -> None:
    before = events.snapshot(conn, path)
    conn.execute("UPDATE documents SET status = 'removed', active = false, updated_at = now() "
                 "WHERE path = %s AND active", (path,))
    facts_mod.deactivate_other_versions(conn, path, None)
    events.emit(conn, path, before, sha=None, status="removed")


def sync_current(conn, current: dict[str, str], reader: FileReader, structured: Path, confidence_map: dict,
                 force: bool = False, llm=None, process=process_file) -> dict:
    """Bring kb in line with the tracker's current file set (used at start-up: the tracker's baseline poll emits no
    events). Only versions kb has not processed yet are read, unless force is set. Mail paths are left alone, whoever
    calls this. Each file commits with what `process` did to it, so its event is final when it becomes visible."""
    known = {(r["path"], r["sha256"]) for r in conn.execute("SELECT path, sha256 FROM documents WHERE active")}
    counts: dict[str, int] = {}
    for path, sha in sorted(current.items()):
        if is_mail(path) or ((path, sha) in known and not force):
            continue
        try:
            status = process(conn, path, sha, reader(sha, path), structured, confidence_map, llm)
        except (psycopg.OperationalError, psycopg.InterfaceError):
            raise   # not the file's fault
        except Exception as exc:
            conn.rollback()
            facts_mod.upsert_document(conn, path, sha, container=None, status="failed",
                                      detail={"error": f"{type(exc).__name__}: {exc}"})
            status = "failed"
            log.warning("failed to process %s: %s", path, exc)
        conn.commit()
        counts[status] = counts.get(status, 0) + 1
    for row in conn.execute("SELECT DISTINCT path FROM documents WHERE active").fetchall():
        if row["path"] not in current and not is_mail(row["path"]):
            remove_path(conn, row["path"])
            counts["removed"] = counts.get("removed", 0) + 1
    conn.commit()
    return counts
