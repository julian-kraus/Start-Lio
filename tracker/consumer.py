"""Subscribe to change events from another process or container.

    from tracker.consumer import subscribe
    for event in subscribe("plan-agent", paths=["suppliers/*/notices/*"], kinds=["added", "changed"]):
        handle(event)   # the cursor advances only after handle() returns and the loop asks for the next one

Delivery is at-least-once: if the consumer crashes mid-handler, that event is delivered again on restart.
Events missed while the consumer was down are caught up from the database before listening.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from fnmatch import fnmatchcase

from . import db


@dataclass(frozen=True)
class Event:
    id: int
    at: datetime
    poll_id: int | None
    kind: str
    path: str
    old_path: str | None
    old_sha: str | None
    new_sha: str | None
    summary: str
    diff: dict | None


def _matches(row: dict, paths: tuple[str, ...], kinds: frozenset[str]) -> bool:
    if kinds and row["kind"] not in kinds:
        return False
    if paths and not any(fnmatchcase(row["path"], p) or (row["old_path"] and fnmatchcase(row["old_path"], p))
                         for p in paths):
        return False
    return True


def subscribe(name: str, *, paths: Iterable[str] = (), kinds: Iterable[str] = (), start: str = "latest",
              database_url: str | None = None, idle_timeout: float = 30.0) -> Iterator[Event]:
    """Yield matching events forever. start='latest' skips history on first run, 'beginning' replays it."""
    url = database_url or os.environ["DATABASE_URL"]
    paths_t, kinds_f = tuple(paths), frozenset(kinds)
    conn = db.connect(url, autocommit=True)
    try:
        db.init_schema(conn)
        cursor = db.get_cursor(conn, name, start)
        conn.execute(f"LISTEN {db.NOTIFY_CHANNEL}")
        while True:
            rows = db.fetch_events(conn, cursor, limit=200)
            for row in rows:
                if _matches(row, paths_t, kinds_f):
                    yield Event(**{k: row[k] for k in Event.__dataclass_fields__})
                cursor = row["id"]
                db.set_cursor(conn, name, cursor)
            if not rows:
                # Block until the tracker commits new events (or the timeout passes, as a safety net).
                for _ in conn.notifies(timeout=idle_timeout, stop_after=1):
                    pass
    finally:
        conn.close()
