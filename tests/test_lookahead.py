"""Lookahead record log, fold, live filter, clock, rehearsal gating and HTTP routes. No mail.
The scenario clock tests use a throwaway plan schema on TEST_DATABASE_URL."""

import json
import os
import re
import threading
import urllib.error
import urllib.request
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from psycopg import sql

from plan import clock as plan_clock
from plan import contracts
from plan import db as plan_db
from tracker import lookahead_view
from tracker.lookahead import Lookahead, LookaheadError, _cli, append_records, fold
from tracker.lookahead_clock import PlanClock
from tracker.web import App, make_handler

FIXTURE = Path(__file__).parent / "fixtures" / "lookahead_rehearsal.jsonl"


def recs(*items):
    return [{**r, "id": i} for i, r in enumerate(items, 1)]


def test_fold_upserts_and_keeps_score_history():
    st = fold(recs(
        {"kind": "supplier", "supplier_id": "S1", "name": "A", "score": {"value": 0.71}, "at": "t1", "cause": "first"},
        {"kind": "supplier", "supplier_id": "S1", "persona": "cheap"},
        {"kind": "supplier", "supplier_id": "S1", "score": {"value": 0.59}, "at": "t2", "cause": "cancelled"},
        {"kind": "promise", "promise_id": "P", "state": "waiting", "what": "loaded"},
        {"kind": "promise", "promise_id": "P", "state": "proven"},
    ))
    s = st["suppliers"]["S1"]
    assert s["name"] == "A" and s["persona"] == "cheap" and s["score"]["value"] == 0.59
    assert [h["value"] for h in s["history"]] == [0.71, 0.59]
    assert st["promises"]["P"] == {"kind": "promise", "promise_id": "P", "state": "proven", "what": "loaded", "id": 5}


def test_latest_plan_replaces_previous():
    st = fold(recs({"kind": "plan", "lanes": [{"lane_id": "a"}]}, {"kind": "plan", "lanes": []}))
    assert st["plan"]["lanes"] == []


@pytest.mark.parametrize("bad", [{"kind": "nope"}, {"kind": "event", "title": "x"}, {"kind": "decision"}, []])
def test_validate_rejects(tmp_path, bad):
    with pytest.raises(LookaheadError):
        append_records(tmp_path / "log.jsonl", [bad])
    assert not (tmp_path / "log.jsonl").exists()


def test_tailer_skips_bad_lines_and_waits_for_complete_lines(tmp_path):
    log = tmp_path / "log.jsonl"
    log.write_text('{"kind": "clock", "now": "2026-11-21T18:00:00+01:00"}\nnot json\n{"kind": "clock", "now": "2026')
    la = Lookahead(log)
    assert [r["kind"] for r in la.records] == ["clock"]
    with open(log, "a") as f:
        f.write('-11-22T06:00:00+01:00"}\n')
    la._read_new()
    assert la.records[-1]["now"].startswith("2026-11-22") and la.records[-1]["id"] == 2


def test_live_mode_uses_wall_clock(tmp_path):
    la = Lookahead(tmp_path / "log.jsonl")
    la.append([{"kind": "clock", "now": "2000-01-01T00:00:00+00:00"}])
    st = la.state()
    assert st["mode"] == "live" and not st["clock"]["simulated"] and not st["clock"]["now"].startswith("2000")
    with pytest.raises(LookaheadError):
        la.advance()


def test_rehearsal_waits_for_goal_and_decision(tmp_path):
    la = Lookahead(tmp_path / "log.jsonl", FIXTURE)
    status = la.state()["rehearsal"]
    assert status["applied"] == 1 and status["blocked_by"]
    with pytest.raises(LookaheadError, match="goal"):
        la.advance()
    la.append([{"kind": "goal", "statement": "all"}])
    la.after_human_step()
    while True:
        try:
            la.advance()
        except LookaheadError as exc:
            assert "D-NOWAK-CONFIRM" in str(exc)
            break
    assert la.state()["decisions"][0]["status"] == "open"
    la.record_decision("D-NOWAK-CONFIRM", "accepted", "Paul Winter", "t")
    la.after_human_step()
    st = la.state()
    assert st["plan"]["coverage"]["met"] is True
    assert st["rehearsal"]["next"] is None
    la.reset()
    assert la.state()["rehearsal"]["applied"] == 1 and la.state()["goal"] is None


def test_fixture_numbers_are_consistent():
    """The rehearsal story must add up: gap, stake, hedge cost and remaining amount."""
    rows = [json.loads(line) for line in FIXTURE.read_text().splitlines()]
    plan = next(r for r in rows if r["kind"] == "plan")
    art = next(l for l in plan["lanes"] if l["lane_id"] == "CMP-GAM-008")
    assert art["need"] - art["usable"] == art["gap"] == 5900
    assert plan["coverage"]["at_stake_eur"] == art["gap"] * 120
    dec = next(r for r in rows if r["kind"] == "decision")
    assert dec["cost"]["eur"] == 8000 * 2.10 * 0.8
    ghost = next(b for r in rows if r["kind"] == "plan" for l in r["lanes"] for b in l["bars"] if b["kind"] == "ghost")
    assert ghost["usable_at"] > art["needed_by"]


@pytest.fixture
def server(tmp_path, serve):
    return serve(Lookahead(tmp_path / "log.jsonl", FIXTURE))


def call(base, path, body=None):
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="GET" if body is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"null") if "json" in r.headers.get("Content-Type", "") else r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_http_flow(server):
    status, page = call(server, "/lookahead")
    assert status == 200 and b"Lookahead" in page
    assert call(server, "/api/lookahead/advance", {})[0] == 409
    assert call(server, "/api/lookahead/goal", {"answers": {"bogus": 1}})[0] == 400
    assert call(server, "/api/lookahead/goal", {"answers": {"unit_loss": "abc"}})[0] == 400
    status, _ = call(server, "/api/lookahead/goal", {"answers": {"goal": "All Collector's", "unit_loss": "120"}})
    assert status == 200
    st = call(server, "/api/lookahead/state")[1]
    assert st["goal"]["unit_loss_eur"] == 120 and st["plan"]["coverage"]["needed"] == 25200
    ev = st["events"][-1]
    assert call(server, f"/api/lookahead/events/{ev['id']}")[1]["title"] == ev["title"]
    while call(server, "/api/lookahead/advance", {})[0] == 200:
        pass
    assert call(server, "/api/lookahead/decisions/D-NOWAK-CONFIRM/accept", {"approver": "agent"})[0] == 400
    assert call(server, "/api/lookahead/decisions/NOPE/accept", {"approver": "Paul Winter"})[0] == 404
    status, body = call(server, "/api/lookahead/decisions/D-NOWAK-CONFIRM/accept", {"approver": "Paul Winter"})
    assert status == 200 and body["sent"] == []
    assert call(server, "/api/lookahead/decisions/D-NOWAK-CONFIRM/accept", {"approver": "Paul Winter"})[0] == 409
    st = call(server, "/api/lookahead/state")[1]
    assert st["plan"]["coverage"]["met"] and st["clock"]["simulated"]
    assert call(server, "/api/lookahead/clock", {"now": "yesterday"})[0] == 400
    assert call(server, "/api/lookahead/clock", {"now": "2026-11-22T07:00:00+01:00"})[0] == 200
    assert call(server, "/api/lookahead/state")[1]["clock"]["now"] == "2026-11-22T07:00:00+01:00"
    assert call(server, "/api/events")[0] == 503  # portal feed needs the database; lookahead does not


# ---------- U1: split, live records, plan clock, goal bar, ghost bar ----------

ROOT = Path(__file__).resolve().parent.parent
T18, T06, T09 = "2030-03-02T18:00:00+01:00", "2030-03-03T06:00:00+01:00", "2030-03-03T09:00:00+01:00"
DEADLINE = "2030-03-03T20:00:00+01:00"  # synthetic


@pytest.fixture(autouse=True)
def no_frozen_clock(monkeypatch):
    monkeypatch.delenv(plan_clock.ENV_VAR, raising=False)


@pytest.fixture
def no_mail(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("mail must not be reached")
    monkeypatch.setattr("mail.tools.MailTools", refuse)


@pytest.fixture
def serve(tmp_path):  # live or rehearsal, any number per test
    running = []
    def start(la):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(App(None, tmp_path, la)))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        running.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    yield start
    for httpd in running:  # also when the test failed
        httpd.shutdown()
        httpd.server_close()


def test_screen_files_are_within_400_lines():
    files = [*(ROOT / "tracker").glob("lookahead*.*"), *(ROOT / "tracker" / "lookahead_static").iterdir(),
             ROOT / "tracker" / "web.py", Path(__file__)]
    assert {f.name: n for f in files if (n := len(f.read_text().splitlines())) > 400} == {}


def test_every_split_file_is_linked_and_served(server):
    # the classic page and the Grand Plan Lio page (/lookahead/v2) share the static folder
    page = call(server, "/lookahead")[1].decode() + call(server, "/lookahead/v2")[1].decode()
    linked = re.findall(r'(?:src|href)="(/lookahead/static/[^"]+)"', page)
    on_disk = {f.name for f in (ROOT / "tracker" / "lookahead_static").iterdir()}
    assert {p.rsplit("/", 1)[1] for p in linked} == on_disk
    for path in linked:
        with urllib.request.urlopen(server + path, timeout=5) as r:
            assert r.status == 200 and r.read()
            assert r.headers["Content-Type"].startswith("text/css" if path.endswith("css") else "text/javascript")


@pytest.mark.parametrize("name", ["nope.js", "../web.py", "..%2Fweb.py", "base.py", "x/../../web.py"])
def test_static_route_serves_nothing_else(server, name):
    assert call(server, "/lookahead/static/" + name)[0] == 404


def event(title, **more):
    return {"kind": "event", "type": "agent", "title": title, **more}


def test_live_mode_shows_only_agent_and_monitor_records(tmp_path, serve):
    path = tmp_path / "log.jsonl"
    append_records(path, [event("unsigned"), event("other", producer="someone"), event("near", producer="agents")])
    append_records(path, [event("rehearsed")], "rehearsal")
    append_records(path, [event("hedger", producer="agent:hedger")])
    append_records(path, [event("monitor"), event("agent", producer="agent")], "monitor")
    append_records(path, [{"kind": "launch", "mode": "rehearse"}], "agent")
    la = Lookahead(path)
    st = la.state()
    assert [e["title"] for e in st["events"]] == ["hedger", "monitor", "agent"]
    assert st["mode"] == "live" and st["launch"] is None and st["hidden_records"] == 5
    assert [r["id"] for r in la.since(0)] == [5, 6, 7]
    stream = la.subscribe()
    la.append([event("hidden", producer="rehearsal"), event("new", producer="monitor")])
    assert stream.get_nowait()["title"] == "new" and stream.empty()
    base = serve(la)
    assert call(base, "/api/lookahead/events/1")[0] == 404
    assert call(base, "/api/lookahead/events/5")[1]["title"] == "hedger"


def test_rehearsal_log_is_invisible_in_live_mode(tmp_path, serve, no_mail):
    path = tmp_path / "log.jsonl"
    rehearsal = Lookahead(path, FIXTURE)
    while rehearsal._beats_applied() < len(rehearsal._script):
        rehearsal.advance(force=True)
    rehearsal.append([{"kind": "launch", "mode": "live"}, {"kind": "goal", "statement": "rehearsed"}])
    assert rehearsal.state()["mode"] == "rehearse" and rehearsal.state()["decisions"]
    live = Lookahead(path)
    st = live.state()
    assert st["mode"] == "live" and st["launch"] is None and st["plan"] is None
    assert not (st["goal"] or st["events"] or st["decisions"] or st["suppliers"] or st["promises"])
    did = rehearsal.state()["decisions"][0]["decision_id"]
    assert call(serve(live), f"/api/lookahead/decisions/{did}/accept", {"approver": "Test Person"})[0] == 404


def test_cli_names_the_producer(tmp_path):
    record, path = tmp_path / "r.json", tmp_path / "log.jsonl"
    record.write_text(json.dumps(event("from the cli")))
    assert _cli(["append", str(path), str(record), "monitor"]) == 0
    assert Lookahead(path).state()["events"][0]["title"] == "from the cli"


def test_goal_bar_follows_the_latest_plan_record(tmp_path):
    la = Lookahead(tmp_path / "log.jsonl")

    def bar(**coverage):
        la.append([{"kind": "plan", "coverage": {"needed": 400, "unit": "units", **coverage}}], "monitor")
        return la.state()["plan"]["coverage"]

    short = bar(covered=100)
    assert (short["fraction"], short["met"], short["hedged"]) == (0.25, False, False)
    hedged = bar(covered=400, hedged=True)
    assert (hedged["fraction"], hedged["met"], hedged["hedged"]) == (1, False, True)
    assert bar(covered=400)["met"] is True
    assert bar(covered=300, met=True)["met"] is False  # never green while short
    la.append([{"kind": "plan", "coverage": {"covered": 0, "needed": 400}, "producer": "rehearsal"}])
    assert la.state()["plan"]["coverage"]["covered"] == 300
    la.append([{"kind": "plan", "lanes": []}], "monitor")
    assert la.state()["plan"]["coverage"] is None  # no figure from an older plan


@pytest.mark.parametrize("lands,needed_by,late,after", [
    ("2030-03-03T21:00:00+01:00", T09, True, DEADLINE),
    ("2030-03-03T21:00:00+01:00", None, True, DEADLINE),
    ("2030-03-03T10:00:00+01:00", T09, True, T09),
    ("2030-03-03T19:30:00Z", None, True, DEADLINE),
    (DEADLINE, None, False, None),
    ("2030-03-03T08:00:00+01:00", T09, False, None),
    (None, T09, False, None),
])
def test_ghost_is_late_after_the_deadline(lands, needed_by, late, after):
    ghost = {"id": "g", "kind": "ghost", "supplier_id": "B", "usable_at": lands}
    bars = [ghost, {"id": "b", "kind": "hedge", "supplier_id": "B"}, {"id": "a", "kind": "supply", "supplier_id": "A"}]
    lane = {"lane_id": "L", "bars": bars, "needed_by": needed_by}
    out = lookahead_view.plan_view({"lanes": [lane]}, {"deadline": DEADLINE})["lanes"][0]["bars"]
    assert [b["id"] for b in out] == ["b", "g", "a"]  # next to the hedged bar
    assert (out[1]["late"], out[1]["late_after"]) == (late, after)
    assert out[1]["usable_at"] == lands  # the record's arrival, untouched


def test_rehearsal_key_jumps_the_clock_to_a_late_ghost(tmp_path):
    la = Lookahead(tmp_path / "log.jsonl", FIXTURE)
    ghosts, seen = [], [la.state()["clock"]["now"]]
    while la._beats_applied() < len(la._script):
        la.advance(force=True)
        seen.append(la.state()["clock"]["now"])
        ghosts += [b for lane in (la.state()["plan"] or {"lanes": []})["lanes"] for b in lane["bars"]
                   if b["kind"] == "ghost"]
    assert ghosts and all(g["late"] for g in ghosts)
    assert seen[0][11:16] == "18:00" and "06:00" in [t[11:16] for t in seen]


def test_clock_needs_an_offset(server):
    assert call(server, "/api/lookahead/clock", {"now": "2030-03-03T06:00:00"})[0] == 400
    assert call(server, "/api/lookahead/clock", {"now": "2030-03-03"})[0] == 400
    assert call(server, "/api/lookahead/clock/next", {})[0] == 409


def test_real_time_cannot_be_jumped(tmp_path, serve):
    base = serve(Lookahead(tmp_path / "log.jsonl"))
    assert call(base, "/api/lookahead/clock/next", {})[0] == 409
    assert call(base, "/api/lookahead/clock", {"now": T06})[0] == 409
    assert call(base, "/api/lookahead/clock")[1]["simulated"] is False


@pytest.mark.parametrize("verdict", [{"in_time": False}, {"misses_deadline": True}, {"offer_expired": True}])
def test_ghost_takes_the_timing_models_verdict(verdict):
    ghost = {"kind": "ghost", "state": "counterfactual", "start": T06, "usable_at": T09, **verdict}
    bars = [{"id": "a", "kind": "supply"}, {"id": "b", "kind": "hedge"}, {"id": "c", "kind": "supply"}, ghost]
    out = lookahead_view.lane_view({"lane_id": "L", "bars": bars}, DEADLINE)["bars"]
    assert out[2]["late"] and out[2]["label"] and out[2]["usable_at"] == T09


def test_clock_outage_is_shown_and_blocks_accept(tmp_path, serve, no_mail):
    la = Lookahead(tmp_path / "log.jsonl", clock=PlanClock("postgresql://nobody:none@127.0.0.1:1/none"))
    la.append([{"kind": "decision", "decision_id": "D", "status": "open"}], "agent")
    assert la.state()["clock"]["error"]
    assert call(serve(la), "/api/lookahead/decisions/D/accept", {"approver": "Test Person"})[0] == 503
    assert la.decision("D")["status"] == "open"


def test_without_the_plan_package_live_runs_on_real_time(monkeypatch):
    monkeypatch.setattr("tracker.lookahead_clock._plan_clock", lambda: None)
    clock = PlanClock()
    assert clock.read()[1] is False
    with pytest.raises(LookaheadError) as refused:
        clock.move(T06)
    assert refused.value.status == 503


@pytest.fixture
def plan_conn(monkeypatch):
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    schema = f"plan_u1_{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("PLAN_SCHEMA", schema)
    conn = plan_db.connect(url, schema, autocommit=True)
    plan_db.migrate(conn, schema)
    screen = PlanClock(url)
    yield conn, screen
    if screen._conn:
        screen._conn.close()
    conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
    conn.close()


def test_one_key_jumps_the_shared_plan_clock(tmp_path, serve, plan_conn):
    conn, screen = plan_conn
    la = Lookahead(tmp_path / "log.jsonl", clock=screen)
    launch = {"kind": "launch", "deadline": DEADLINE, "demo_times": [T09, T18, T06]}
    contracts.validate_record(launch)
    la.append([launch], "agent:planner")
    base = serve(la)
    assert call(base, "/api/lookahead/clock/next", {})[0] == 409  # real time

    plan_clock.set_scenario(conn, T18)
    seen = call(base, "/api/lookahead/state")[1]["clock"]
    assert seen["simulated"] and plan_clock.parse(seen["now"]) == plan_clock.parse(T18)
    assert plan_clock.parse(seen["next"]) == plan_clock.parse(T06)
    for expected in (T06, T09):
        assert call(base, "/api/lookahead/clock/next", {})[0] == 200
        assert plan_clock.now(conn) == plan_clock.parse(expected)  # what every other module reads
        assert plan_clock.parse(call(base, "/api/lookahead/clock")[1]["now"]) == plan_clock.parse(expected)
    assert call(base, "/api/lookahead/clock/next", {})[0] == 409  # no later stop
    assert call(base, "/api/lookahead/clock", {"now": T06})[0] == 409  # never backwards
    assert call(base, "/api/lookahead/clock", {"now": DEADLINE})[0] == 200
    assert plan_clock.now(conn) == plan_clock.parse(DEADLINE)
    assert all(r["kind"] != "clock" for r in la.records)  # moved in plan.clock, not in the log
    plan_clock.set_scenario(conn, T18, rate=2)
    la.move_clock()
    assert plan_clock.read(conn)["rate"] == 2


def test_human_steps_are_stamped_with_scenario_time(tmp_path, serve, plan_conn):
    conn, screen = plan_conn
    la = Lookahead(tmp_path / "log.jsonl", clock=screen)
    la.append([{"kind": "launch", "goal_questions": [{"id": "goal", "q": "Goal?"}]}], "agent")
    plan_clock.set_scenario(conn, T06)
    assert call(serve(la), "/api/lookahead/goal", {"answers": {"goal": "All on time"}})[0] == 200
    goal = la.state()["goal"]
    assert goal["producer"] == "app" and plan_clock.parse(goal["at"]) == plan_clock.parse(T06)
    contracts.validate_record({k: v for k, v in goal.items() if k != "id"})
