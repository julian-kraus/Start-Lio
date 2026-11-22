"""Event loop on the tracker's change feed and on mail events: ingest, then reconcile once per batch."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import psycopg

from tracker import db as tracker_db

from . import db, mail_source, reconcile
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


def _tracker_tables(conn) -> None:
    """Until commit or rollback, unqualified names are the tracker's: kb's search_path would shadow public."""
    conn.execute("SET LOCAL search_path TO public")


def _current_sha(conn, path: str) -> str | None:
    row = conn.execute("SELECT sha256 FROM public.file_versions WHERE path = %s AND superseded_at IS NULL",
                       (path,)).fetchone()
    return row["sha256"] if row else None


def _ingest(conn, path: str, sha: str, data: bytes, structured: Path, confidence_map: dict, llm=None) -> str:
    """A portal file. Facts it reuses from an email lose the mail label before their event can be read."""
    since = mail_source.last_fact_event(conn)
    status = process_file(conn, path, sha, data, structured, confidence_map, llm)
    mail_source.release_copies(conn, path)
    mail_source.announce_stored_types(conn, path, since)
    return status


def sync(conn, settings: KbSettings, reader: FileReader, confidence_map: dict, *, llm=None,
         force: bool = False) -> dict:
    """The current-file sync of `kb run` and `kb sync`."""
    counts = sync_current(conn, current_sha_by_path(conn, settings.data_dir), reader, settings.structured_dir,
                          confidence_map, force=force, llm=llm, process=_ingest)
    mail_source.release_copies(conn)
    conn.commit()
    return counts


def extract(conn, settings: KbSettings, reader: FileReader, llm, mail: mail_source.MailSource, confidence_map: dict,
            *, limit: int = 20) -> tuple[dict, list[dict]]:
    """The queue drain of `kb run` and `kb extract`: what it did, and the mail it parked because no kind fits."""
    def read(sha: str, path: str) -> bytes:
        return mail.read(sha) if mail_source.is_mail(path) else reader(sha, path)

    drained = drain(conn, mail_source.LabellingClient(llm, mail, conn), read, settings.structured_dir, confidence_map,
                    limit=limit)
    parked = mail_source.park_unrouted(conn)
    conn.commit()
    return drained, parked


def handle_event(conn, ev: dict, reader: FileReader, settings: KbSettings, confidence_map: dict,
                 llm=None) -> str | None:
    """Events can arrive late or twice; only the tracker's current version of a path is ever ingested."""
    kind, path = ev["kind"], ev["path"]
    if mail_source.is_mail(path):
        return None   # mail/ belongs to kb.mail_source; the portal cannot add or remove a supplier's email
    current = _current_sha(conn, path)
    if kind in ("added", "changed", "renamed"):
        if kind == "renamed" and ev.get("old_path") and not mail_source.is_mail(ev["old_path"]):
            remove_path(conn, ev["old_path"])
        sha = ev["new_sha"]
        if sha != current:
            return None
        return _ingest(conn, path, sha, reader(sha, path), settings.structured_dir, confidence_map, llm)
    if kind == "removed":
        if current is not None:
            return None
        remove_path(conn, path)
        return "removed"
    return None   # stale / recovered: read from the tracker's stale_flags during reconcile


def catch_up(conn, settings: KbSettings, reader: FileReader, llm,
             mail: mail_source.MailSource) -> dict | None:
    """Process all events after the cursor in batches; reconcile once if anything happened."""
    confidence_map = _confidence_map(conn)
    # A new cursor starts at the latest event: the start-up sync covers everything that happened before.
    processed, paths = 0, []
    following = db.tracker_table_exists(conn, "events")   # without a tracker there is still mail to consume
    if following:
        _tracker_tables(conn)
        cursor = tracker_db.get_cursor(conn, CONSUMER, start="latest")
    conn.commit()
    while following:
        _tracker_tables(conn)
        rows = tracker_db.fetch_events(conn, cursor, BATCH)
        conn.commit()
        if not rows:
            break
        for ev in rows:
            try:
                status = handle_event(conn, ev, reader, settings, confidence_map, llm)
                if status:
                    paths.append({"path": ev["path"], "event": ev["kind"], "status": status})
            except Exception as exc:
                conn.rollback()
                log.warning("event %s (%s %s) failed: %s", ev["id"], ev["kind"], ev["path"], exc)
            cursor = ev["id"]
            _tracker_tables(conn)
            tracker_db.set_cursor(conn, CONSUMER, cursor)
            conn.commit()
            processed += 1
    stale_events = processed and not paths
    paths.extend(mail.catch_up(conn, settings.structured_dir, confidence_map))
    if llm is not None:
        drained, parked = extract(conn, settings, reader, llm, mail, confidence_map)
        if drained["done"]:
            paths.append({"llm_extracted": drained["done"]})
        paths.extend({"path": r["path"], "status": "failed"} for r in parked)
    if paths or stale_events:
        return reconcile.run(conn, "file_events", {"events": processed, "paths": paths[:200]})
    conn.commit()
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


def _follow(conn, listen, settings: KbSettings, reader: FileReader, llm, mail: mail_source.MailSource,
            stop: threading.Event, idle_timeout: float) -> None:
    _tracker_tables(conn)
    tracker_db.init_schema(conn)
    tracker_db.get_cursor(conn, CONSUMER, start="latest")   # before the sync: a drop during it has a higher id
    conn.commit()
    load_contract(conn, settings.structured_dir)
    counts = sync(conn, settings, reader, _confidence_map(conn), llm=llm)
    log.info("start-up sync: %s", counts)
    result = reconcile.run(conn, "startup_sync", {"sync": counts})
    log.info("reconcile run %s: %s issues", result["run_id"], result["issues"])
    listen.execute(f"LISTEN {tracker_db.NOTIFY_CHANNEL}")
    listen.execute(f"LISTEN {mail_source.CHANNEL}")
    while not stop.is_set():
        result = catch_up(conn, settings, reader, llm, mail)
        if result:
            log.info("reconcile run %s: %s issues, changes %s", result["run_id"], result["issues"],
                     {k: len(v) for k, v in result["changes"].items()})
        for _ in listen.notifies(timeout=idle_timeout, stop_after=1):
            pass


def run(settings: KbSettings, stop: threading.Event, idle_timeout: float = 5.0) -> None:
    """Follow the tracker's change feed and mail events holding the single-worker lock. A lost connection backs off,
    reconnects, re-takes the lock and resumes from the stored cursors; a worker that cannot take the lock waits and
    retries."""
    reader = FileReader(settings.data_dir)
    llm = load_adapter(settings.llm_adapter)
    mail = mail_source.MailSource.from_env()
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
            _follow(conn, listen, settings, reader, llm, mail, stop, idle_timeout)
        except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
            wait = RECONNECT_BACKOFF[min(failures, len(RECONNECT_BACKOFF) - 1)]
            failures += 1
            log.warning("kb db connection lost (%s); reconnecting in %ss", type(exc).__name__, wait)
            stop.wait(wait)
        finally:
            _close(conn)
            _close(listen)
