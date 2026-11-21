"""CLI: python -m tracker <command>."""

from __future__ import annotations

import argparse
import json
import logging
import random
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import psycopg

from . import db, notify, poller
from .client import AuthError, PortalClient
from .config import PROJECT_ROOT, load_settings, parse_duration
from .store import BlobStore

log = logging.getLogger("tracker")
MAX_CONSECUTIVE_FAILURES = 10

try:
    from zoneinfo import ZoneInfo

    LOCAL_TZ = ZoneInfo("Europe/Berlin")
except Exception:  # no tz database in the image
    LOCAL_TZ = timezone.utc


def _local(ts: datetime | None) -> str:
    return ts.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S %Z") if ts else "-"


def _parse_time(text: str) -> datetime:
    """ISO timestamp, or a relative duration like '2h' meaning 'that long ago'."""
    try:
        return datetime.fromtimestamp(time.time() - parse_duration(text), timezone.utc)
    except ValueError:
        ts = datetime.fromisoformat(text)
        return ts if ts.tzinfo else ts.replace(tzinfo=LOCAL_TZ)


def cmd_run(args, settings) -> int:
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    interval = parse_duration(args.interval) if args.interval else settings.poll_interval
    client = PortalClient(settings.base_url, settings.token)
    store = BlobStore(settings.data_dir)
    conn = None
    reconciled = False
    failures = 0
    while not stop.is_set():
        started = time.monotonic()
        ok = False
        try:
            if conn is None or conn.closed or conn.broken:
                conn = db.connect(settings.database_url)
                db.init_schema(conn)
                if not db.try_poller_lock(conn):
                    conn.commit()
                    conn.close()
                    conn = None
                    if args.once:
                        log.error("another tracker is already polling; not running a second one")
                        return 2
                    log.warning("another tracker holds the poller lock; waiting")
                    stop.wait(10)
                    continue
                conn.commit()
            if not reconciled:
                fixed = poller.reconcile_current(store, db.current_state(conn))
                conn.commit()
                if fixed:
                    log.info("restored %d files in the current view", fixed)
                reconciled = True
            baseline = not db.has_baseline(conn)
            conn.commit()
            result = poller.poll_once(settings, client, store, conn, baseline=baseline)
            if result.baseline:
                log.info("baseline recorded: %d files, no events emitted", result.files)
            else:
                log.info("poll %d: %d files, %d events", result.poll_id, result.files, len(result.events))
            if result.failed:
                log.warning("poll %d: %d files skipped and retried next poll: %s", result.poll_id,
                            len(result.failed), ", ".join(f["path"] for f in result.failed[:5]))
            if settings.webhook_urls:
                notify.dispatch_webhooks(conn, settings.webhook_urls)
            ok = True
        except AuthError as exc:
            log.error("%s; check SOONY_API_TOKEN", exc)
        except psycopg.OperationalError as exc:
            log.error("database unavailable: %s", exc)
            conn = None
        except Exception:
            log.exception("poll failed")
        failures = 0 if ok else failures + 1
        if failures >= MAX_CONSECUTIVE_FAILURES:
            # Exit so the container's restart policy gives us a clean process (a wedged poller is worse).
            log.error("%d polls failed in a row, exiting", failures)
            return 1
        if args.once:
            return 0 if ok else 1
        stop.wait(max(1.0, interval * random.uniform(0.9, 1.1) - (time.monotonic() - started)))
    if conn is not None:
        conn.close()
    return 0


def cmd_baseline(args, settings) -> int:
    client = PortalClient(settings.base_url, settings.token)
    store = BlobStore(settings.data_dir)
    with db.connect(settings.database_url) as conn:
        db.init_schema(conn)
        if not db.try_poller_lock(conn):
            print("another tracker is polling; stop it before recording a baseline", file=sys.stderr)
            return 2
        if db.has_baseline(conn) and not args.force:
            print("a baseline already exists; use --force to record another baseline poll", file=sys.stderr)
            return 1
        conn.commit()
        remote = client.list_files()
        imported = poller.import_local(store, Path(args.source), remote) if args.source else 0
        result = poller.poll_once(settings, client, store, conn, baseline=True)
        print(f"baseline poll {result.poll_id}: {result.files} files ({imported} imported from {args.source}, "
              f"rest downloaded), no events emitted")
    return 0


def cmd_import_history(args, settings) -> int:
    snapshot = json.loads(Path(args.manifest).read_text())
    label = args.label or f"history import: {Path(args.manifest).name}"
    store = BlobStore(settings.data_dir)
    with db.connect(settings.database_url) as conn:
        db.init_schema(conn)
        try:
            result = poller.import_history(store, conn, snapshot, Path(args.root), label, settings.diff_max_items)
        except poller.AlreadyImported as exc:
            print(exc, file=sys.stderr)
            return 1
    print(f"{label}: {len(result.events)} events added to the history"
          + (f", {len(result.failed)} paths skipped" if result.failed else ""), file=sys.stderr)
    return 0


def cmd_export(args, settings) -> int:
    from .export import export

    with db.connect(settings.database_url) as conn:
        stats = export(conn, BlobStore(settings.data_dir), Path(args.out),
                       _parse_time(args.since) if args.since else None)
    print(f"exported {stats['events']} events, {stats['files_tracked']} files to {args.out}", file=sys.stderr)
    return 0


def cmd_changes(args, settings) -> int:
    clauses, params = [], []
    if args.since:
        clauses.append("at >= %s")
        params.append(_parse_time(args.since))
    if args.path:
        clauses.append("(path LIKE %s OR old_path LIKE %s)")
        pattern = args.path.replace("%", r"\%").replace("*", "%").replace("?", "_")
        params += [pattern, pattern]
    if args.kind:
        clauses.append("kind = ANY(%s)")
        params.append(args.kind.split(","))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with db.connect(settings.database_url) as conn:
        rows = conn.execute(
            f"SELECT id, at, kind, path, summary FROM events {where} ORDER BY id DESC LIMIT %s",
            (*params, args.limit),
        ).fetchall()
    if args.json:
        print(json.dumps([{**r, "at": r["at"].isoformat()} for r in rows], ensure_ascii=False, indent=2))
        return 0
    for r in reversed(rows):
        print(f"#{r['id']:<5} {_local(r['at'])}  {r['kind']:<9} {r['path']}\n       {r['summary']}")
    if not rows:
        print("no matching events")
    return 0


def cmd_show(args, settings) -> int:
    with db.connect(settings.database_url) as conn:
        row = conn.execute("SELECT * FROM events WHERE id = %s", (args.id,)).fetchone()
    if not row:
        print(f"no event {args.id}", file=sys.stderr)
        return 1
    print(json.dumps({**row, "at": row["at"].isoformat()}, ensure_ascii=False, indent=2))
    return 0


def cmd_history(args, settings) -> int:
    with db.connect(settings.database_url) as conn:
        rows = conn.execute(
            "SELECT version, sha256, bytes, first_seen, superseded_at FROM file_versions WHERE path = %s ORDER BY version",
            (args.path,),
        ).fetchall()
    for r in rows:
        until = _local(r["superseded_at"]) if r["superseded_at"] else "current"
        print(f"v{r['version']:<3} {r['sha256'][:12]}  {r['bytes']:>10,} B  {_local(r['first_seen'])} → {until}")
    if not rows:
        print("no history for this path")
    return 0


def cmd_get(args, settings) -> int:
    at = _parse_time(args.at) if args.at else datetime.now(timezone.utc)
    with db.connect(settings.database_url) as conn:
        row = conn.execute(
            """SELECT version, sha256 FROM file_versions WHERE path = %s AND first_seen <= %s
               AND (superseded_at IS NULL OR superseded_at > %s) ORDER BY version DESC LIMIT 1""",
            (args.path, at, at),
        ).fetchone()
    if not row:
        print("no version of that path existed at that time", file=sys.stderr)
        return 1
    data = BlobStore(settings.data_dir).read(row["sha256"])
    if args.output:
        Path(args.output).write_bytes(data)
        print(f"wrote v{row['version']} to {args.output}", file=sys.stderr)
    else:
        sys.stdout.buffer.write(data)
    return 0


def cmd_status(args, settings) -> int:
    with db.connect(settings.database_url) as conn:
        last = conn.execute("SELECT * FROM polls ORDER BY id DESC LIMIT 1").fetchone()
        ok = db.last_successful_poll(conn)
        counts = conn.execute(
            "SELECT (SELECT count(*) FROM file_versions WHERE superseded_at IS NULL) AS files, "
            "(SELECT count(*) FROM file_versions) AS versions, (SELECT count(*) FROM events) AS events"
        ).fetchone()
    print(f"last poll: #{last['id'] if last else '-'} ok={last['ok'] if last else '-'} "
          f"finished {_local(last['finished_at'] if last else None)}"
          + (f" error: {last['error']}" if last and last["error"] else ""))
    print(f"last successful poll: {_local(ok)}")
    print(f"files: {counts['files']}, versions kept: {counts['versions']}, events: {counts['events']}")
    return 0


def cmd_healthcheck(args, settings) -> int:
    max_age = parse_duration(args.max_age) if args.max_age else settings.poll_interval * 3 + 60
    try:
        with db.connect(settings.database_url) as conn:
            ok = db.last_successful_poll(conn)
    except psycopg.Error:
        return 1
    return 0 if ok and (datetime.now(timezone.utc) - ok).total_seconds() < max_age else 1


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(prog="tracker", description="Track changes on the Soony portal.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="poll the portal (forever, or once)")
    p.add_argument("--once", action="store_true")
    p.add_argument("--interval", help="e.g. 60s, 5m (default POLL_INTERVAL)")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("baseline", help="record the current portal state without emitting events")
    p.add_argument("--from", dest="source", default=str(PROJECT_ROOT / "data" / "raw"),
                   help="local mirror to import instead of downloading (default data/raw)")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_baseline)

    p = sub.add_parser("import-history",
                       help="fold an older snapshot (manifest + files) into the history and emit what changed since")
    p.add_argument("--manifest", default=str(PROJECT_ROOT / "data" / "manifest.json"),
                   help="JSON: {path: {sha256, bytes, first_seen}} (default data/manifest.json)")
    p.add_argument("--root", default=str(PROJECT_ROOT / "data" / "raw"), help="folder holding the snapshot files")
    p.add_argument("--label", help="unique name for this import (makes it idempotent)")
    p.set_defaults(func=cmd_import_history)

    p = sub.add_parser("export", help="write events, history, inventory and before/after files to a folder")
    p.add_argument("--out", required=True, help="new folder to create")
    p.add_argument("--since", help="only events since (ISO time or duration ago)")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("changes", help="list change events")
    p.add_argument("--since", help="ISO time or duration ago, e.g. 2h")
    p.add_argument("--path", help="glob, e.g. 'suppliers/*/notices/*'")
    p.add_argument("--kind", help="comma-separated: added,changed,removed,renamed,stale,recovered")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_changes)

    p = sub.add_parser("show", help="full diff of one event")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("history", help="all versions of a file")
    p.add_argument("path")
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("get", help="content of a file as of a point in time")
    p.add_argument("path")
    p.add_argument("--at", help="ISO time or duration ago (default now)")
    p.add_argument("-o", "--output")
    p.set_defaults(func=cmd_get)

    sub.add_parser("status", help="last poll and totals").set_defaults(func=cmd_status)

    p = sub.add_parser("healthcheck", help="exit 0 if the last successful poll is recent")
    p.add_argument("--max-age")
    p.set_defaults(func=cmd_healthcheck)

    args = parser.parse_args(argv)
    return args.func(args, load_settings())


if __name__ == "__main__":
    sys.exit(main())
