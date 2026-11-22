"""The shared plan clock as the Lookahead screen uses it in live mode.

Live mode shows the time of `plan.clock`: real time, or scenario time while a scenario is running. Every module
reads that clock, so moving it here moves the monitor and the agents too. Only a running scenario clock can be
moved, and only forward. The demo times come from the launch record (`demo_times`, ISO times with offset).
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone

import psycopg

from . import db
from .lookahead import LookaheadError

log = logging.getLogger("tracker.lookahead")
CONNECT_TIMEOUT = 3


def parse_time(value) -> datetime:
    try:
        at = datetime.fromisoformat(str(value))
    except ValueError:
        raise LookaheadError(400, f"not an ISO timestamp: {value!r}") from None
    if at.tzinfo is None:
        raise LookaheadError(400, f"time needs a UTC offset: {value!r}")
    return at


def next_stop(stops: list, now: datetime) -> datetime | None:
    return min((t for t in map(parse_time, stops) if t > now), default=None)


def _plan_clock():
    # imported on use: the screen and the portal feed also run where the plan package is not installed
    try:
        from plan import clock
    except ImportError:
        return None
    return clock


class PlanClock:
    def __init__(self, url: str | None = None):
        self.url = url
        self._conn: psycopg.Connection | None = None
        self._lock = threading.Lock()  # one connection, and one move of the clock at a time
        self._clock = _plan_clock()
        if not self._clock:
            log.warning("plan package not installed: live mode runs on real time")

    def _connection(self) -> psycopg.Connection | None:
        if not self.url:
            return None
        if self._conn is None or self._conn.closed or self._conn.broken:
            url = psycopg.conninfo.make_conninfo(self.url, connect_timeout=CONNECT_TIMEOUT)
            self._conn = db.connect(url, autocommit=True)
        return self._conn

    def read(self) -> tuple[datetime, bool]:
        """(now, scenario running). Raises 503 instead of passing real time off as scenario time."""
        if not self._clock:
            return datetime.now(timezone.utc), False
        with self._lock:
            try:
                conn = self._connection()
                return self._clock.now(conn), self._clock.is_scenario(conn)
            except (psycopg.Error, self._clock.ClockError) as exc:
                raise self._unavailable(exc) from exc

    def move(self, to=None, stops: list | None = None) -> datetime:
        """Move a running scenario clock forward, to `to` or to the first of `stops` still ahead."""
        if not self._clock:
            raise LookaheadError(503, "the plan package is not installed")
        with self._lock:
            try:
                conn = self._connection()
                if conn is None or self._clock.read(conn)["mode"] != "scenario":
                    raise LookaheadError(409, "no scenario clock is running: real time cannot be moved")
                if os.environ.get(self._clock.ENV_VAR, "").strip():
                    raise LookaheadError(409, f"the clock is frozen by {self._clock.ENV_VAR}")
                now = self._clock.now(conn)
                target = parse_time(to) if to is not None else next_stop(stops or [], now)
                if target is None:
                    raise LookaheadError(409, "the launch record has no later demo time")
                if target <= now:
                    raise LookaheadError(409, "the scenario clock only moves forward")
                rate = float(self._clock.read(conn)["rate"])
                self._clock.set_scenario(conn, target, rate=rate, by="lookahead screen")
                return target
            except (psycopg.Error, self._clock.ClockError) as exc:
                raise self._unavailable(exc) from exc

    def _unavailable(self, exc: Exception) -> LookaheadError:
        log.warning("cannot use the plan clock: %s", exc)
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        return LookaheadError(503, "the plan clock is unavailable")
