"""Routes of the Agent rules view.

GET  /api/lookahead/rules           the saved rules and the number of saves
GET  /api/lookahead/rules/signers   who can sign what, read-only from kb.current_facts
POST /api/lookahead/rules           {autonomy, hedge, escalation, based_on}; appends a `rules` record
"""

from __future__ import annotations

import logging
import os
from zoneinfo import ZoneInfo

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from . import db, lookahead_rules
from .lookahead import Lookahead, LookaheadError
from .lookahead_clock import parse_time

log = logging.getLogger("tracker.lookahead")


def policy_facts(url: str) -> list[dict]:
    query = sql.SQL("SELECT " + lookahead_rules.COLUMNS + " FROM {}.current_facts").format(
        sql.Identifier(os.environ.get("KB_SCHEMA", "kb")))
    with db.connect(url) as conn:
        conn.read_only = True
        with conn.cursor(row_factory=dict_row) as cur:
            return cur.execute(query).fetchall()


def team_day(la: Lookahead):
    """The day in the team's time zone: it decides which policy, chart and holder are valid."""
    now = parse_time(la.now())
    zone = (la.state()["launch"] or {}).get("timezone")
    try:
        return (now.astimezone(ZoneInfo(zone)) if zone else now).date()
    except (KeyError, ValueError):
        raise LookaheadError(409, f"the launch record has an unknown time zone: {zone!r}") from None


def get_rules(routes, la, match, params, body):
    return lookahead_rules.saved(la)


def post_rules(routes, la, match, params, body):
    return lookahead_rules.save(la, body)


def get_signers(routes, la, match, params, body):
    url = getattr(la.clock, "url", None)
    if not url:
        return lookahead_rules.unavailable("database")
    day = team_day(la)
    try:
        rows = policy_facts(url)
    except psycopg.Error as exc:
        log.warning("cannot read the policy facts: %s", type(exc).__name__)
        return lookahead_rules.unavailable("database")
    return lookahead_rules.signers(rows, day)


ROUTES = [
    ("GET", r"/api/lookahead/rules", get_rules),
    ("GET", r"/api/lookahead/rules/signers", get_signers),
    ("POST", r"/api/lookahead/rules", post_rules),
]
