"""Write one agent run and every call it makes to the `plan` schema.

A run is one row in `plan.runs` (kind, the clock time it saw, whether a scenario clock was running, its input and,
at the end, its output or error). Each model turn and each tool execution the run makes is one row in
`plan.agent_traces`, numbered by `step`, so the whole decision can be replayed from the database: which tools were
called with what input, what they returned, which model answered, how many tokens it cost, and whether the
validator guard accepted the verdict.

The tables are owned by issue R4 (plan schema, additive contract). This module only inserts rows.
"""

from __future__ import annotations

import json
from typing import Any

from psycopg import sql
from psycopg.types.json import Json

from plan import clock, db


def _jsonable(obj: Any) -> Any:
    """Make an arbitrary value safe for jsonb: anything json cannot encode becomes its string form."""
    return json.loads(json.dumps(obj, default=str, ensure_ascii=False))


class Tracer:
    """Opens a `plan.runs` row and appends `plan.agent_traces` rows for one agent run.

    Each write commits on its own so a crash still leaves the trace up to the last call (the trace is a log, not
    part of the caller's transaction). Pass a connection already pointed at the plan schema (`plan.db.connect`).
    """

    def __init__(self, conn, *, role: str, kind: str = "agent", scenario: bool | None = None,
                 model: str | None = None, input: Any = None):
        self.conn = conn
        self.role = role
        self.model = model
        self._step = 0
        self._closed = False
        now = clock.now(conn)
        scen = clock.is_scenario(conn) if scenario is None else scenario
        row = conn.execute(
            sql.SQL("INSERT INTO {} (kind, status, scenario, clock_now, input) "
                    "VALUES (%s, 'running', %s, %s, %s) RETURNING run_id").format(sql.Identifier("runs")),
            (kind, scen, now, Json(_jsonable(input if input is not None else {})))).fetchone()
        conn.commit()
        self.run_id = row["run_id"]

    def next_step(self) -> int:
        """The step number to group one model turn and the tool calls it triggered under."""
        self._step += 1
        return self._step

    def trace(self, *, step: int, tool: str | None = None, input: Any = None, output: Any = None,
              model: str | None = None, tokens_in: int | None = None, tokens_out: int | None = None,
              verdict_ok: bool | None = None, error: str | None = None) -> None:
        self.conn.execute(
            sql.SQL("INSERT INTO {} (run_id, role, step, tool, input, output, model, tokens_in, tokens_out, "
                    "verdict_ok, error) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)")
            .format(sql.Identifier("agent_traces")),
            (self.run_id, self.role, step, tool,
             Json(_jsonable(input if input is not None else {})),
             None if output is None else Json(_jsonable(output)),
             model if model is not None else self.model, tokens_in, tokens_out, verdict_ok, error))
        self.conn.commit()

    def finish(self, status: str, *, output: Any = None, error: str | None = None) -> None:
        if self._closed:
            return
        self._closed = True
        self.conn.execute(
            sql.SQL("UPDATE {} SET status = %s, finished_at = now(), output = %s, error = %s WHERE run_id = %s")
            .format(sql.Identifier("runs")),
            (status, None if output is None else Json(_jsonable(output)), error, self.run_id))
        self.conn.commit()

    def rows(self) -> list[dict]:
        """Every trace row for this run, in step order; for tests and for the monitor to render a run."""
        return list(self.conn.execute(
            sql.SQL("SELECT step, tool, input, output, model, tokens_in, tokens_out, verdict_ok, error "
                    "FROM {} WHERE run_id = %s ORDER BY id").format(sql.Identifier("agent_traces")),
            (self.run_id,)))


def open_tracer(url: str | None = None, *, role: str, **kwargs) -> Tracer:
    """Convenience: connect to the plan schema, make sure it is migrated, and start a tracer."""
    conn = db.connect(url or _plan_url())
    db.migrate(conn)
    conn.commit()
    return Tracer(conn, role=role, **kwargs)


def _plan_url() -> str:
    import os
    url = os.environ.get("PLAN_DATABASE_URL") or os.environ.get("DATABASE_URL") or os.environ.get("TEST_DATABASE_URL")
    if not url:
        raise SystemExit("no PLAN_DATABASE_URL / DATABASE_URL set for agent traces")
    return url
