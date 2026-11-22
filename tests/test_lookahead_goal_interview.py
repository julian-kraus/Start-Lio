"""The goal screen's POST handler runs the goal interview: a read-back that stores nothing, then a confirm that
stores the goal in plan.goals through agents.interview_store and appends the records that put it on the timeline.

Synthetic editions, numbers and a replay adapter; the stored cases use a throwaway Postgres schema."""

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from psycopg import sql

from agents import interview as iv
from agents import llm
from agents.interview_text import one_line
from plan import clock, db
from tracker import lookahead
from tracker import lookahead_web as web

URL = os.environ.get("TEST_DATABASE_URL")
DEMO = json.loads((Path(__file__).resolve().parent / "fixtures" / "replay" / "interview_goal.json").read_text())
ZONE = "Europe/Berlin"
NOW = datetime(2030, 1, 12, 17, 0, tzinfo=timezone.utc)      # a Saturday, 18:00 in Berlin
SUNDAY = "2030-01-13T10:00:00+01:00"
INPUTS = {"label": "demo pre-fill", "unit_loss": "60", "budget": "700", "team_window": {"to": SUNDAY}}


def replay():
    """A fresh adapter per request: the live client is stateless across requests, the replay one is not."""
    return llm.ReplayClient(DEMO["script"])


class FakeStores:
    """Only the plan connection the goal handler needs, on the test schema (PLAN_SCHEMA is set by the fixture)."""

    def plan(self):
        return db.connect(URL)


@pytest.fixture
def plan_conn(monkeypatch):
    if not URL:
        pytest.skip("TEST_DATABASE_URL not set")
    schema = f"plan_test_{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("PLAN_SCHEMA", schema)
    monkeypatch.setenv(clock.ENV_VAR, NOW.isoformat())
    c = db.connect(URL, schema)
    try:
        db.migrate(c, schema)
        c.commit()
        yield c
    finally:
        c.rollback()
        c.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
        c.commit()
        c.close()


def screen_with_launch(tmp_path, prefill=True):
    """A rehearsal waiting on the goal, its launch carrying the editions and (optionally) the pre-fill the
    interview reads. Without the pre-fill the value of a missing copy is an open point."""
    launch = {"kind": "launch", "name": "Example", "mode": "rehearse", "deadline": SUNDAY, "timezone": ZONE,
              "product": "PROD-X", "editions": DEMO["editions"], "goal_questions": iv.questions()}
    if prefill:
        launch["goal_inputs"] = INPUTS
    script = tmp_path / "script.jsonl"
    script.write_text("".join(json.dumps(r) + "\n" for r in [
        launch,
        {"kind": "cue", "label": "Goal interview", "wait_for": "goal"},
        {"kind": "plan", "coverage": {"covered": 5, "needed": 9, "unit": "copies"}}]))
    return lookahead.Lookahead(tmp_path / "log.jsonl", script)


def test_read_back_shows_the_goal_and_stores_nothing(plan_conn, tmp_path):
    screen = screen_with_launch(tmp_path)
    out = web.lookahead_goal(screen, {"answers": DEMO["answers"]}, client=replay(), stores=FakeStores())
    assert out["confirmed"] is False and out["ready"] is True
    assert "Collector's" in "\n".join(out["show"])         # the read-back is the structured goal, not raw answers
    assert plan_conn.execute("SELECT count(*) AS n FROM goals").fetchone()["n"] == 0
    assert screen.state()["goal"] is None                  # nothing on the timeline before confirmation


def test_open_points_are_shown_and_block_a_confirm(plan_conn, tmp_path):
    screen = screen_with_launch(tmp_path, prefill=False)    # no pre-fill: the value of a missing copy is open
    out = web.lookahead_goal(screen, {"answers": DEMO["answers"]}, client=replay(), stores=FakeStores())
    assert out["confirmed"] is False and out["ready"] is False
    assert any("missing copy" in line for line in out["show"])
    with pytest.raises(lookahead.LookaheadError) as exc:
        web.lookahead_goal(screen, {"answers": DEMO["answers"], "author": "Test Person"},
                           client=replay(), stores=FakeStores())
    assert exc.value.status == 400
    assert plan_conn.execute("SELECT count(*) AS n FROM goals").fetchone()["n"] == 0
    assert screen.state()["goal"] is None


def test_confirm_stores_the_goal_with_author_and_appends_its_records(plan_conn, tmp_path):
    screen = screen_with_launch(tmp_path)
    out = web.lookahead_goal(screen, {"answers": DEMO["answers"], "author": "Test Person"},
                             client=replay(), stores=FakeStores())
    assert out["confirmed"] is True
    row = plan_conn.execute("SELECT created_by, created_at FROM goals WHERE superseded_at IS NULL").fetchone()
    assert row["created_by"] == "Test Person" and row["created_at"] == NOW
    state = screen.state()
    assert state["goal"]["statement"] == one_line(DEMO["answers"]["goal"])
    assert state["goal"]["producer"] == "agent:goal_interview"   # a live screen hides records without a producer
    assert state["goal"]["author"] == "Test Person"
    assert state["events"][-1]["type"] == "goal"
    assert state["plan"]["coverage"]["needed"] == 9             # the beat that waited on the goal was released


@pytest.mark.parametrize("author", ["", "   ", "agent:planner"])
def test_a_goal_is_confirmed_only_by_a_named_person(plan_conn, tmp_path, author):
    screen = screen_with_launch(tmp_path)
    with pytest.raises(lookahead.LookaheadError) as exc:
        web.lookahead_goal(screen, {"answers": DEMO["answers"], "author": author},
                           client=replay(), stores=FakeStores())
    assert exc.value.status == 400
    assert plan_conn.execute("SELECT count(*) AS n FROM goals").fetchone()["n"] == 0
    assert screen.state()["goal"] is None


def test_unknown_answers_are_refused(plan_conn, tmp_path):
    screen = screen_with_launch(tmp_path)
    with pytest.raises(lookahead.LookaheadError) as exc:
        web.lookahead_goal(screen, {"answers": {"not_a_question": "x"}}, client=replay(), stores=FakeStores())
    assert exc.value.status == 400
