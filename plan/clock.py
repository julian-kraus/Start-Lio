"""One clock for every module: `clock.now()` returns real time, or scenario time when a scenario is running.

Scenario time comes from, in order:
  1. the environment variable LOOKAHEAD_SCENARIO_NOW (an ISO 8601 time with offset), frozen at that instant;
  2. the single row of `plan.clock` when a connection is passed: mode 'scenario' gives
     scenario_now + rate * (real time since set_at); rate 0 freezes the clock, rate 1 lets it run in real time.
Otherwise it is the real UTC time, also where the plan schema is not migrated. Always timezone-aware.
Any module's connection works: the table is named with its schema (`PLAN_SCHEMA`, default `plan`).
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Callable

from psycopg import sql
from psycopg.rows import dict_row

from plan import db

ENV_VAR = "LOOKAHEAD_SCENARIO_NOW"


class ClockError(ValueError):
    pass


def _real() -> datetime:
    return datetime.now(timezone.utc)


def parse(value: str) -> datetime:
    """Parse an ISO 8601 time; a naive time is refused because it hides the offset."""
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ClockError(f"not an ISO 8601 time: {value!r}") from exc
    if dt.tzinfo is None:
        raise ClockError(f"time needs a UTC offset: {value!r}")
    return dt


def scenario_time(scenario_now: datetime, rate: float, set_at: datetime, real_now: datetime) -> datetime:
    """Pure: where the scenario clock stands at real_now."""
    return scenario_now + timedelta(seconds=rate * max(0.0, (real_now - set_at).total_seconds()))


def _table() -> sql.Identifier:
    return sql.Identifier(db.schema_name(), "clock")


def read(conn) -> dict:
    """The stored clock row (mode, scenario_now, rate, set_at, set_by)."""
    table = _table()
    row = None
    with conn.cursor(row_factory=dict_row) as cur:
        # look before selecting: a failed select would abort the caller's transaction
        if cur.execute("SELECT to_regclass(%s) AS t", (table.as_string(conn),)).fetchone()["t"]:
            row = cur.execute(sql.SQL("SELECT mode, scenario_now, rate, set_at, set_by FROM {} WHERE id = 1")
                              .format(table)).fetchone()
    return row if row else {"mode": "real", "scenario_now": None, "rate": 0.0, "set_at": None, "set_by": None}


def now(conn=None, real: Callable[[], datetime] = _real) -> datetime:
    env = os.environ.get(ENV_VAR, "").strip()
    if env:
        return parse(env)
    if conn is not None:
        row = read(conn)
        if row["mode"] == "scenario":
            return scenario_time(row["scenario_now"], float(row["rate"]), row["set_at"], real())
    return real()


def is_scenario(conn=None) -> bool:
    if os.environ.get(ENV_VAR, "").strip():
        return True
    return conn is not None and read(conn)["mode"] == "scenario"


def set_scenario(conn, at: datetime | str, rate: float = 0.0, by: str | None = None,
                 real: Callable[[], datetime] = _real) -> None:
    """Start or move the scenario clock. The caller commits."""
    at = parse(at) if isinstance(at, str) else at
    if at.tzinfo is None:
        raise ClockError("scenario time needs a UTC offset")
    if rate < 0:
        raise ClockError("rate must be >= 0")
    conn.execute(sql.SQL("INSERT INTO {} (id, mode, scenario_now, rate, set_at, set_by) VALUES (1, 'scenario', %s, %s, %s, %s) "
                 "ON CONFLICT (id) DO UPDATE SET mode = EXCLUDED.mode, scenario_now = EXCLUDED.scenario_now, "
                 "rate = EXCLUDED.rate, set_at = EXCLUDED.set_at, set_by = EXCLUDED.set_by").format(_table()),
                 (at, rate, real(), by))


def set_real(conn, by: str | None = None, real: Callable[[], datetime] = _real) -> None:
    """Back to real time. The caller commits."""
    conn.execute(sql.SQL("INSERT INTO {} (id, mode, scenario_now, rate, set_at, set_by) VALUES (1, 'real', NULL, 0, %s, %s) "
                 "ON CONFLICT (id) DO UPDATE SET mode = 'real', scenario_now = NULL, rate = 0, "
                 "set_at = EXCLUDED.set_at, set_by = EXCLUDED.set_by").format(_table()), (real(), by))
