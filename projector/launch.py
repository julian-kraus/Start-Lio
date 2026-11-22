"""Project the Lookahead screen from kb, the goal record and the plan clock, deterministically and idempotently.

`project()` is pure: given the kb facts, the goal and the clock it returns the list of records for that state.
`run()` loads those inputs from a kb connection, writes the new records and, with `--follow`, re-projects on
every change. `write_new()` keys records by content so writing the same state twice adds nothing: an unchanged
record is skipped, and a changed fact writes a new record that supersedes the old one, because the upsert kinds
(supplier, decision, promise) fold by key and the other kinds are additive.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path

from plan import clock as plan_clock
from sourcing.facts import Facts, load as load_facts
from tracker import lookahead

from . import risk, suppliers

PRODUCER = "projector"
_VOLATILE = ("producer", "id")


def content_key(record: dict) -> str:
    """A stable key for a record's content, ignoring the producer stamp and the log position."""
    body = {k: v for k, v in record.items() if k not in _VOLATILE}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _read_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def goal_of(records: list[dict]) -> dict | None:
    """The goal record already on the screen, or None. The goal comes from the goal interview, not from kb."""
    folded = lookahead.fold([{**record, "id": n} for n, record in enumerate(records, 1)])
    return folded.get("goal")


def _deadline(goal: dict | None) -> datetime | None:
    """The deadline that bounds coverage. It is a launch input carried on the goal record; absent, coverage
    is left to a later run once the goal interview has set it. No deadline is ever assumed."""
    value = (goal or {}).get("deadline")
    if not value:
        return None
    return value if isinstance(value, datetime) else plan_clock.parse(str(value))


def project(current: list[dict], facts: Facts, goal: dict | None, *, now: datetime,
            deadline: datetime | None) -> list[dict]:
    """Every Lookahead record for the current kb state. Pure: the same inputs give the same records.

    Supplier records always project from the history. The plan record projects when a deadline is known, from
    coverage and the weak-spot ranking.
    """
    records: list[dict] = suppliers.records(current, facts.believed("received_qty"))
    if deadline is not None:
        coverage = risk.coverage_of(facts, now=now, deadline=deadline)
        spots = risk.weak_spots(facts, coverage, goal)
        records.append(risk.plan_record(coverage, spots))
    return records


def write_new(path: Path, records: list[dict], producer: str = PRODUCER) -> list[dict]:
    """Append only the records this producer has not written before. Returns the records appended."""
    path = Path(path)
    seen = {content_key(r) for r in _read_log(path) if r.get("producer") == producer}
    fresh, batch = [], set()
    for record in records:
        key = content_key(record)
        if key in seen or key in batch:
            continue
        batch.add(key)
        fresh.append(record)
    if fresh:
        lookahead.append_records(path, fresh, producer)
    return fresh


def project_once(log_path: Path, conn) -> list[dict]:
    """Read one kb snapshot, project it and write the new records."""
    log_path = Path(log_path)
    facts = load_facts(conn)
    current = list(facts.by_id.values())
    goal = goal_of(_read_log(log_path))
    now = plan_clock.now(conn)
    records = project(current, facts, goal, now=now, deadline=_deadline(goal))
    return write_new(log_path, records)


def run(log_path, url, *, once: bool = True, interval: float = 2.0, schema: str | None = None) -> list[dict]:
    """Project the screen from the kb at `url`. `once` projects the current state and returns; otherwise it
    re-projects every `interval` seconds, writing only what changed, until the process is stopped."""
    from kb import db as kb_db

    schema = schema or os.environ.get("KB_SCHEMA", "kb")
    conn = kb_db.connect(url, schema, autocommit=True)
    try:
        if once:
            return project_once(log_path, conn)
        while True:
            project_once(log_path, conn)
            time.sleep(interval)
    finally:
        conn.close()
    return []
