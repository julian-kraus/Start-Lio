"""Turn one file version into facts: tables through schema maps, documents through existing envelopes or the
extraction queue. Idempotent per (path, sha256), so at-least-once events and re-syncs are safe."""

from __future__ import annotations

import logging
from pathlib import Path

from tracker.diff import detect_kind
from tracker.store import BlobStore

from . import events
from . import facts as facts_mod
from .loader import DEFAULT_CONFIDENCE, envelope_for, load_envelope
from .tabular import TABULAR_CONTAINERS, find_header, guess_header, read_tables, table_to_facts

log = logging.getLogger(__name__)


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
    return {r["signature"]: r["mapping"] for r in conn.execute("SELECT signature, mapping FROM schema_maps")}


def process_file(conn, path: str, sha: str, data: bytes, structured: Path,
                 confidence_map: dict | None = None) -> str:
    """Returns the document status. Emits a fact event when the path's valid facts changed."""
    before = events.snapshot(conn, path)
    status = _process_file(conn, path, sha, data, structured, confidence_map)
    events.emit(conn, path, before, sha=sha, status=status)
    return status


def _process_file(conn, path: str, sha: str, data: bytes, structured: Path,
                  confidence_map: dict | None = None) -> str:
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
        parsed, unmatched, stats = [], [], {}
        for table in tables:
            if not table.rows:
                continue
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
            if container == "xlsx":
                conn.execute("UPDATE facts SET valid = false WHERE source_sha256 = %s AND extractor <> 'kb:tabular' "
                             "AND locator->>'sheet' = ANY(%s)", (sha, parsed))
            else:
                conn.execute("UPDATE facts SET valid = false WHERE source_sha256 = %s AND extractor <> 'kb:tabular'",
                             (sha,))
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
        detail = {**detail, "parsed": stats, "unmatched": unmatched}
        facts_mod.upsert_document(conn, path, sha, container=container, status=status,
                                  extractor="kb:tabular" if parsed else None, detail=detail)
        return status

    if facts_mod.has_valid_facts(conn, sha):
        status = "loaded"
    else:
        env = envelope_for(structured, sha)
        if env is not None:
            load_envelope(conn, env, current_sha=sha, confidence_map=confidence_map, emit=False)
            status = "loaded"
        else:
            conn.execute(
                "INSERT INTO extraction_queue (path, sha256, container) VALUES (%s, %s, %s) "
                "ON CONFLICT (path, sha256) DO UPDATE SET state = CASE WHEN extraction_queue.state = 'superseded' "
                "THEN 'pending' ELSE extraction_queue.state END, updated_at = now()", (path, sha, container))
            status = "queued"
    existing = conn.execute("SELECT status FROM documents WHERE path = %s AND sha256 = %s", (path, sha)).fetchone()
    facts_mod.upsert_document(conn, path, sha, container=container,
                              status=existing["status"] if existing and status == "loaded" else status, detail=detail)
    return status


def remove_path(conn, path: str) -> None:
    before = events.snapshot(conn, path)
    conn.execute("UPDATE documents SET status = 'removed', active = false, updated_at = now() "
                 "WHERE path = %s AND active", (path,))
    facts_mod.deactivate_other_versions(conn, path, None)
    events.emit(conn, path, before, sha=None, status="removed")


def sync_current(conn, current: dict[str, str], reader: FileReader, structured: Path, confidence_map: dict,
                 force: bool = False) -> dict:
    """Bring kb in line with the tracker's current file set (used at start-up: the tracker's baseline poll emits no
    events). Only versions kb has not processed yet are read, unless force is set."""
    known = {(r["path"], r["sha256"]) for r in conn.execute("SELECT path, sha256 FROM documents WHERE active")}
    counts: dict[str, int] = {}
    for path, sha in sorted(current.items()):
        if (path, sha) in known and not force:
            continue
        try:
            status = process_file(conn, path, sha, reader(sha, path), structured, confidence_map)
        except Exception as exc:
            conn.rollback()
            facts_mod.upsert_document(conn, path, sha, container=None, status="failed",
                                      detail={"error": f"{type(exc).__name__}: {exc}"})
            status = "failed"
            log.warning("failed to process %s: %s", path, exc)
        conn.commit()
        counts[status] = counts.get(status, 0) + 1
    for row in conn.execute("SELECT DISTINCT path FROM documents WHERE active").fetchall():
        if row["path"] not in current:
            remove_path(conn, row["path"])
            counts["removed"] = counts.get("removed", 0) + 1
    conn.commit()
    return counts
