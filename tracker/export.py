"""Export the change history for people outside the system (analysts, designers):
events with full diffs, the version history, an inventory of tracked files, and the before/after
versions of every file an event touched."""

from __future__ import annotations

import csv
import json
import shutil
from datetime import date, datetime
from pathlib import Path, PurePosixPath

from . import db, diff
from .store import BlobStore


def _default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def _write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, default=_default, ensure_ascii=False, indent=2))


def export(conn, store: BlobStore, out: Path, since: datetime | None = None) -> dict:
    out.mkdir(parents=True, exist_ok=False)
    where, args = ("WHERE at >= %s", (since,)) if since else ("", ())
    events = conn.execute(f"SELECT * FROM events {where} ORDER BY id", args).fetchall()
    versions = conn.execute(
        "SELECT path, version, sha256, bytes, first_seen, superseded_at FROM file_versions ORDER BY path, version"
    ).fetchall()
    polls = conn.execute(
        """SELECT count(*) AS polls, count(*) FILTER (WHERE ok) AS ok, min(started_at) AS first,
                  max(finished_at) AS last, count(*) FILTER (WHERE baseline) AS baselines
           FROM polls WHERE note IS NULL"""
    ).fetchone()

    # Before/after copies of every file an event refers to.
    files_dir = out / "files"
    for ev in events:
        target = files_dir / f"{ev['id']:04d}_{ev['kind']}"
        copies = {}
        for side, sha, path in (("before", ev["old_sha"], ev["old_path"] or ev["path"]), ("after", ev["new_sha"], ev["path"])):
            if sha and store.has(sha):
                dest = target / side / PurePosixPath(path).name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(store.object_path(sha), dest)
                copies[side] = dest.relative_to(out).as_posix()
        ev["files"] = copies

    _write_json(out / "events.json", events)

    by_path: dict[str, list] = {}
    for v in versions:
        by_path.setdefault(v["path"], []).append(v)
    history = {p: vs for p, vs in by_path.items() if len(vs) > 1 or any(e["path"] == p for e in events)}
    _write_json(out / "file_history.json", history)

    current = db.current_state(conn)
    with (out / "inventory.csv").open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["path", "area", "supplier_folder", "kind", "bytes", "versions", "tracked_since", "events"])
        event_count: dict[str, int] = {}
        for ev in events:
            event_count[ev["path"]] = event_count.get(ev["path"], 0) + 1
        for path, cur in sorted(current.items()):
            parts = path.split("/")
            kind = diff.detect_kind(path, store.read(cur.sha256)) if store.has(cur.sha256) else "unknown"
            writer.writerow([path, parts[0], parts[1] if parts[0] == "suppliers" else "", kind, cur.bytes,
                             len(by_path.get(path, [])), by_path[path][0]["first_seen"].isoformat(),
                             event_count.get(path, 0)])

    stats = {
        "exported_at": datetime.now().astimezone().isoformat(),
        "events": len(events),
        "events_by_kind": {k: sum(1 for e in events if e["kind"] == k) for k in sorted({e["kind"] for e in events})},
        "files_tracked": len(current),
        "versions_kept": len(versions),
        "polls": polls,
    }
    _write_json(out / "stats.json", stats)
    return stats
