"""One poll: list, fetch what changed, diff, record history and events in a single transaction.

Failure isolation: a file that cannot be fetched or parsed is skipped for this poll (and retried on the
next one) instead of failing the poll, so one bad file can never stop tracking of the others.
"""

from __future__ import annotations

import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import db, detectors, diff
from .client import AuthError, PortalClient, RemoteFile
from .config import Settings
from .store import BlobStore, UnsafePath, check_path

log = logging.getLogger(__name__)


@dataclass
class Fetched:
    path: str
    sha256: str
    bytes: int


@dataclass
class PendingEvent:
    kind: str
    path: str
    summary: str
    old_path: str | None = None
    old_sha: str | None = None
    new_sha: str | None = None
    diff: dict | None = None
    at: datetime | None = None


@dataclass
class PollResult:
    poll_id: int
    files: int
    baseline: bool
    events: list[dict] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)


def _fetch(client: PortalClient, store: BlobStore, remote: RemoteFile) -> Fetched:
    """Reuse a blob we already hold intact (e.g. a file reverted to an older version); download otherwise.
    client.download verifies size and checksum, so only content matching the listing is stored."""
    if not store.has(remote.sha256, remote.bytes):
        dl = client.download(remote, store.tmp)
        store.put_file(dl.tmp_path, dl.sha256, dl.bytes)
    return Fetched(remote.path, remote.sha256, remote.bytes)


def fetch_all(client: PortalClient, store: BlobStore, files: list[RemoteFile],
              workers: int) -> tuple[list[Fetched], list[dict]]:
    if not files:
        return [], []

    def one(f: RemoteFile):
        try:
            return _fetch(client, store, f), None
        except AuthError:
            raise
        except Exception as exc:
            log.warning("skipping %s this poll: %s", f.path, exc)
            return None, {"path": f.path, "error": f"{type(exc).__name__}: {exc}"[:500]}

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(files)))) as pool:
        results = list(pool.map(one, files))
    return [r for r, _ in results if r], [e for _, e in results if e]


def reconcile_current(store: BlobStore, state: dict[str, db.CurrentFile]) -> int:
    """Make data/current match the database (after a crash or a wiped volume): restore missing or stale
    files and delete leftovers. Stat calls only; a bad path is logged and skipped, never fatal."""
    fixed = 0
    for path, cur in state.items():
        try:
            if store.has(cur.sha256, cur.bytes) and not store.is_current(path, cur.sha256):
                store.set_current(path, cur.sha256)
                fixed += 1
        except (OSError, ValueError) as exc:
            log.warning("cannot restore current/%s: %s", path, exc)
    for path in store.current_files():
        if path not in state:
            try:
                store.remove_current(path)
                fixed += 1
            except (OSError, ValueError) as exc:
                log.warning("cannot remove leftover current/%s: %s", path, exc)
    return fixed


def _event_for(store: BlobStore, state: dict[str, db.CurrentFile], f: Fetched, renames: dict[str, str],
               max_items: int) -> PendingEvent:
    new_bytes = store.read(f.sha256)
    cur = state.get(f.path)
    if cur is not None:
        summary, detail = diff.compute(f.path, store.read(cur.sha256), f.path, new_bytes, max_items)
        return PendingEvent("changed", f.path, summary, old_sha=cur.sha256, new_sha=f.sha256, diff=detail)
    if f.path in renames:
        old_path = renames[f.path]
        old_sha = state[old_path].sha256
        summary, detail = diff.compute(old_path, store.read(old_sha), f.path, new_bytes, max_items)
        return PendingEvent("renamed", f.path, f"moved from {old_path}; {summary}", old_path=old_path,
                            old_sha=old_sha, new_sha=f.sha256, diff=detail)
    summary, detail = diff.describe(f.path, new_bytes)
    return PendingEvent("added", f.path, summary, new_sha=f.sha256, diff=detail)


def _build_events(store: BlobStore, state: dict[str, db.CurrentFile], fetched: list[Fetched],
                  removed: list[str], max_items: int) -> list[PendingEvent]:
    added = [f.path for f in fetched if f.path not in state]
    renames = detectors.pair_renames(removed, added)
    events = []
    for f in fetched:
        try:
            events.append(_event_for(store, state, f, renames, max_items))
        except Exception as exc:  # diff/describe already guard themselves; this is the last line
            log.warning("could not describe %s: %s", f.path, exc)
            cur = state.get(f.path)
            events.append(PendingEvent("changed" if cur else "added", f.path,
                                       f"content changed (could not read it: {type(exc).__name__})",
                                       old_sha=cur.sha256 if cur else None, new_sha=f.sha256,
                                       diff={"diff_error": str(exc)[:500]}))
    renamed_from = set(renames.values())
    for path in removed:
        if path not in renamed_from:
            cur = state[path]
            events.append(PendingEvent("removed", path, f"removed (version {cur.version} kept in history)",
                                       old_sha=cur.sha256))
    return events


def _emit(written: list[dict]) -> None:
    for ev in written:
        sys.stdout.write(json.dumps(ev, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _write_events(conn, poll_id: int, pending: list[PendingEvent], now: datetime) -> list[dict]:
    written = []
    for ev in pending:
        at = ev.at or now
        event_id = db.insert_event(conn, poll_id, ev.kind, ev.path, ev.summary, old_path=ev.old_path,
                                   old_sha=ev.old_sha, new_sha=ev.new_sha, diff=ev.diff, at=at)
        written.append({"id": event_id, "at": at.isoformat(), "kind": ev.kind, "path": ev.path,
                        "old_path": ev.old_path, "summary": ev.summary})
    return written


def poll_once(settings: Settings, client: PortalClient, store: BlobStore, conn, baseline: bool = False) -> PollResult:
    poll_id = db.start_poll(conn, baseline)
    failed: list[dict] = []
    try:
        remote = []
        for f in client.list_files():
            try:
                check_path(f.path)
                remote.append(f)
            except UnsafePath as exc:
                failed.append({"path": f.path, "error": str(exc)})
        state = db.current_state(conn)
        conn.commit()  # do not hold a transaction open while downloading
        to_fetch = [f for f in remote if f.path not in state or state[f.path].sha256 != f.sha256]
        remote_paths = {f.path for f in remote}
        unsafe = {f["path"] for f in failed}
        removed = [p for p in state if p not in remote_paths and p not in unsafe]
        fetched, fetch_failed = fetch_all(client, store, to_fetch, settings.download_workers)
        failed += fetch_failed
        pending = [] if baseline else _build_events(store, state, fetched, removed, settings.diff_max_items)

        now = datetime.now(timezone.utc)
        last_change = {p: c.first_seen for p, c in state.items() if p in remote_paths}
        last_change.update({f.path: now for f in fetched})
        with conn.transaction():
            db.lock_events(conn)
            for f in fetched:
                db.add_version(conn, f.path, f.sha256, f.bytes, now)
            for path in removed:
                db.remove_version(conn, path, now)
            if not baseline:
                flags = db.stale_flags(conn)
                for f in fetched:
                    if f.path in flags:
                        db.clear_stale_flag(conn, f.path)
                        pending.append(PendingEvent("recovered", f.path,
                                                    f"changed again after being stale since {flags[f.path]:%Y-%m-%d %H:%M} UTC",
                                                    new_sha=f.sha256))
                for path, rule, age in detectors.find_stale(settings.stale_rules, last_change, set(flags), now):
                    db.set_stale_flag(conn, path, now)
                    pending.append(PendingEvent("stale", path, f"no change for {_age(age)} (threshold {_age(rule.seconds)})",
                                                diff={"rule": rule.pattern, "age_seconds": int(age),
                                                      "last_change": last_change[path].isoformat()}))
            written = _write_events(conn, poll_id, pending, now)
            db.finish_poll(conn, poll_id, True, len(remote), len(written), None, failed)
        conn.commit()
    except Exception as exc:
        conn.rollback()
        db.finish_poll(conn, poll_id, False, None, None, f"{type(exc).__name__}: {exc}"[:2000], failed)
        conn.commit()
        raise

    # Update the on-disk view only after the database committed; reconcile_current repairs any gap.
    for f in fetched:
        try:
            store.set_current(f.path, f.sha256)
        except (OSError, ValueError) as exc:
            log.warning("cannot update current/%s: %s", f.path, exc)
    for path in removed:
        try:
            store.remove_current(path)
        except (OSError, ValueError) as exc:
            log.warning("cannot remove current/%s: %s", path, exc)
    _emit(written)
    return PollResult(poll_id, len(remote), baseline, written, failed)


def _age(seconds: float) -> str:
    if seconds < 3600:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


def import_local(store: BlobStore, source: Path, remote: list[RemoteFile]) -> int:
    """Seed the blob store from a local mirror so the baseline needs no downloads for matching files."""
    imported = 0
    for f in remote:
        local = source / f.path
        try:
            check_path(f.path)
            if not store.has(f.sha256, f.bytes) and local.is_file() and local.stat().st_size == f.bytes:
                sha, _ = store.import_file(local)
                imported += sha == f.sha256
        except (OSError, UnsafePath) as exc:
            log.warning("not importing %s: %s", f.path, exc)
    return imported


class AlreadyImported(RuntimeError):
    pass


def import_history(store: BlobStore, conn, snapshot: dict[str, dict], root: Path, label: str,
                   max_items: int = 500) -> PollResult:
    """Fold an older snapshot (path -> {sha256, bytes, first_seen}) into the history as earlier versions,
    and emit the events that happened between that snapshot and the start of tracking.

    For each path, the snapshot is compared with the earliest version the tracker recorded:
      same content    -> that version simply existed earlier (first_seen moves back), no event
      different       -> the snapshot becomes version 1, existing versions shift up, 'changed' event
      unknown to DB   -> the file was removed before tracking started: version 1 + 'removed' event
      not in snapshot -> files present at the baseline without any event yet get an 'added' event
    Idempotent per label."""
    with conn.transaction():
        db.lock_events(conn)
        if conn.execute("SELECT 1 FROM polls WHERE note = %s", (label,)).fetchone():
            raise AlreadyImported(f"history import {label!r} was already applied")
        baseline_at = conn.execute(
            "SELECT min(finished_at) AS t FROM polls WHERE ok AND baseline").fetchone()["t"]
        if baseline_at is None:
            raise RuntimeError("record a baseline first (python -m tracker baseline)")
        poll_id = conn.execute("INSERT INTO polls (baseline, note) VALUES (false, %s) RETURNING id",
                               (label,)).fetchone()["id"]
        earliest = {r["path"]: r for r in conn.execute(
            """SELECT DISTINCT ON (path) path, version, sha256, bytes, first_seen
               FROM file_versions ORDER BY path, version""").fetchall()}
        has_events = {r["path"] for r in conn.execute("SELECT DISTINCT path FROM events").fetchall()}
        pending: list[PendingEvent] = []
        failed: list[dict] = []
        source = {"history_import": label}

        def shift_and_insert(path: str, sha: str, size: int, since: datetime, until: datetime) -> None:
            conn.execute("UPDATE file_versions SET version = -version WHERE path = %s", (path,))
            conn.execute("UPDATE file_versions SET version = -version + 1 WHERE path = %s", (path,))
            conn.execute(
                """INSERT INTO file_versions (path, version, sha256, bytes, first_seen, last_seen, superseded_at)
                   VALUES (%s, 1, %s, %s, %s, %s, %s)""", (path, sha, size, since, since, until))

        for path, old in sorted(snapshot.items()):
            try:
                check_path(path)
                since = datetime.fromisoformat(old["first_seen"])
                if not store.has(old["sha256"], old.get("bytes")):
                    sha, _ = store.import_file(root / path)
                    if sha != old["sha256"]:
                        raise ValueError(f"local copy no longer matches the snapshot (sha {sha[:12]})")
                first = earliest.get(path)
                if first is None:
                    shift_and_insert(path, old["sha256"], old["bytes"], since, baseline_at)
                    pending.append(PendingEvent("removed", path, "removed before tracking started",
                                                old_sha=old["sha256"], diff=source, at=baseline_at))
                elif first["sha256"] == old["sha256"]:
                    if since < first["first_seen"]:
                        conn.execute("UPDATE file_versions SET first_seen = %s WHERE path = %s AND version = %s",
                                     (since, path, first["version"]))
                else:
                    shift_and_insert(path, old["sha256"], old["bytes"], since, first["first_seen"])
                    summary, detail = diff.compute(path, store.read(old["sha256"]), path,
                                                   store.read(first["sha256"]), max_items)
                    pending.append(PendingEvent("changed", path, summary, old_sha=old["sha256"],
                                                new_sha=first["sha256"], diff={**detail, **source},
                                                at=first["first_seen"]))
            except Exception as exc:
                log.warning("history import skipped %s: %s", path, exc)
                failed.append({"path": path, "error": f"{type(exc).__name__}: {exc}"[:500]})

        for path, first in sorted(earliest.items()):
            # Files that appeared after the baseline already got a live 'added' event (has_events).
            if path in snapshot or path in has_events:
                continue
            summary, detail = diff.describe(path, store.read(first["sha256"]))
            pending.append(PendingEvent("added", path, summary, new_sha=first["sha256"],
                                        diff={**detail, **source}, at=first["first_seen"]))

        written = _write_events(conn, poll_id, pending, baseline_at)
        db.finish_poll(conn, poll_id, True, len(snapshot), len(written), None, failed, label)
    conn.commit()
    _emit(written)
    return PollResult(poll_id, len(snapshot), False, written, failed)
