"""Event loop on the tracker's change feed: ingest changed files, then reconcile once per batch."""

from __future__ import annotations

import logging
import threading

import psycopg

from tracker import db as tracker_db

from . import db, reconcile
from .config import KbSettings
from .ingest import FileReader, process_file, remove_path, sync_current
from .intake import drain
from .llm.base import load_adapter
from .loader import DEFAULT_CONFIDENCE, current_sha_by_path, load_contract
from .rules import active_version, load_rule_set

log = logging.getLogger("kb")
CONSUMER = "kb-extraction"
BATCH = 500
WORKER_LOCK = db.MIGRATION_LOCK   # one kb worker follows the feed at a time (AGENTS.md: kb advisory lock)
RECONNECT_BACKOFF = (1, 2, 5, 15, 30)


def _confidence_map(conn) -> dict:
    version = active_version(conn)
    if version is None:
        return DEFAULT_CONFIDENCE
    return load_rule_set(conn, version)[1].get("confidence_map", DEFAULT_CONFIDENCE)


def _current_sha(conn, path: str) -> str | None:
    row = conn.execute("SELECT sha256 FROM public.file_versions WHERE path = %s AND superseded_at IS NULL",
                       (path,)).fetchone()
    return row["sha256"] if row else None


def handle_event(conn, ev: dict, reader: FileReader, settings: KbSettings, confidence_map: dict,
                 llm=None) -> str | None:
    """Events can arrive late or twice; only the tracker's current version of a path is ever ingested."""
    kind, path = ev["kind"], ev["path"]
    current = _current_sha(conn, path)
    if kind in ("added", "changed", "renamed"):
        if kind == "renamed" and ev.get("old_path"):
            remove_path(conn, ev["old_path"])
        sha = ev["new_sha"]
        if sha != current:
            return None
        return process_file(conn, path, sha, reader(sha, path), settings.structured_dir, confidence_map, llm)
    if kind == "removed":
        if current is not None:
            return None
        remove_path(conn, path)
        return "removed"
    return None   # stale / recovered: read from the tracker's stale_flags during reconcile


def catch_up(conn, settings: KbSettings, reader: FileReader, llm) -> dict | None:
    """Process all events after the cursor in batches; reconcile once if anything happened."""
    confidence_map = _confidence_map(conn)
    # Start at the latest event: the start-up sync already covers everything that happened before.
    cursor = tracker_db.get_cursor(conn, CONSUMER, start="latest")
    conn.commit()
    processed, paths = 0, []
    while rows := tracker_db.fetch_events(conn, cursor, BATCH):
        for ev in rows:
            try:
                status = handle_event(conn, ev, reader, settings, confidence_map, llm)
                if status:
                    paths.append({"path": ev["path"], "event": ev["kind"], "status": status})
            except Exception as exc:
                conn.rollback()
                log.warning("event %s (%s %s) failed: %s", ev["id"], ev["kind"], ev["path"], exc)
            cursor = ev["id"]
            tracker_db.set_cursor(conn, CONSUMER, cursor)
            conn.commit()
            processed += 1
    stale_events = processed and not paths
    if llm is not None:
        drained = drain(conn, llm, reader, settings.structured_dir, confidence_map)
        if drained["done"]:
            paths.append({"llm_extracted": drained["done"]})
    if paths or stale_events:
        return reconcile.run(conn, "file_events", {"events": processed, "paths": paths[:200]})
    return None


def _take_lock(conn) -> bool:
    """Session-level advisory lock, released automatically when the connection drops: only one kb worker follows
    the feed, so two workers (or a worker that lost its connection and a restart) never process the same event."""
    return conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (WORKER_LOCK,)).fetchone()["ok"]


def _close(conn) -> None:
    if conn is not None and not conn.closed:
        try:
            conn.close()
        except Exception:
            pass


def _follow(conn, listen, settings: KbSettings, reader: FileReader, llm, stop: threading.Event,
            idle_timeout: float) -> None:
    tracker_db.init_schema(conn)
    conn.commit()
    load_contract(conn, settings.structured_dir)
    counts = sync_current(conn, current_sha_by_path(conn, settings.data_dir), reader, settings.structured_dir,
                          _confidence_map(conn), llm=llm)
    log.info("start-up sync: %s", counts)
    result = reconcile.run(conn, "startup_sync", {"sync": counts})
    log.info("reconcile run %s: %s issues", result["run_id"], result["issues"])
    listen.execute(f"LISTEN {tracker_db.NOTIFY_CHANNEL}")
    while not stop.is_set():
        result = catch_up(conn, settings, reader, llm)
        if result:
            log.info("reconcile run %s: %s issues, changes %s", result["run_id"], result["issues"],
                     {k: len(v) for k, v in result["changes"].items()})
        for _ in listen.notifies(timeout=idle_timeout, stop_after=1):
            pass


def run(settings: KbSettings, stop: threading.Event, idle_timeout: float = 5.0) -> None:
    """Follow the tracker's change feed holding the single-worker lock. A lost connection backs off, reconnects,
    re-takes the lock and resumes from the stored cursor; a worker that cannot take the lock waits and retries."""
    reader = FileReader(settings.data_dir)
    llm = load_adapter(settings.llm_adapter)
    failures = 0
    while not stop.is_set():
        conn = listen = None
        try:
            conn = db.connect(settings.database_url, settings.schema)
            if not _take_lock(conn):
                wait = RECONNECT_BACKOFF[min(failures, len(RECONNECT_BACKOFF) - 1)]
                failures += 1
                log.info("another kb worker holds the lock; waiting %ss", wait)
                _close(conn)
                conn = None
                stop.wait(wait)
                continue
            listen = db.connect(settings.database_url, settings.schema, autocommit=True)
            failures = 0
            _follow(conn, listen, settings, reader, llm, stop, idle_timeout)
        except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
            wait = RECONNECT_BACKOFF[min(failures, len(RECONNECT_BACKOFF) - 1)]
            failures += 1
            log.warning("kb db connection lost (%s); reconnecting in %ss", type(exc).__name__, wait)
            stop.wait(wait)
        finally:
            _close(conn)
            _close(listen)
