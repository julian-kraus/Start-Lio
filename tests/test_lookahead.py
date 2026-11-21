"""Lookahead record log, fold, rehearsal gating and HTTP routes. No database, no mail."""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from tracker.lookahead import Lookahead, LookaheadError, append_records, fold
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
def server(tmp_path):
    la = Lookahead(tmp_path / "log.jsonl", FIXTURE)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(App(None, tmp_path, la)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


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
