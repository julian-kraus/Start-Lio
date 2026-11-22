"""The goal and clock endpoints write only records that the plan contracts accept. Synthetic values."""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from plan import contracts
from tracker.lookahead import Lookahead, LookaheadError, append_records
from tracker.web import App, make_handler

LAUNCH = {"kind": "launch", "name": "Test launch", "goal_questions": [
    {"id": key, "q": key} for key in ("goal", "unit_loss", "budget", "note")]}


@pytest.fixture
def screen(tmp_path):
    script, log = tmp_path / "script.jsonl", tmp_path / "log.jsonl"
    script.write_text(json.dumps(LAUNCH) + "\n")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(App(None, tmp_path, Lookahead(log, script))))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def call(route, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{httpd.server_address[1]}/api/lookahead/{route}",
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    yield call, log
    httpd.shutdown()
    httpd.server_close()


@pytest.mark.parametrize("answers", [
    *({key: value} for key in ("unit_loss", "budget")
      for value in ("-5", -0.01, "nan", "inf", "-inf", "1e999", float("nan"))),
    {"note": float("nan")},
    {"note": {"nested": [float("inf")]}},
    {"goal": 0},
    {"goal": None},
])
def test_goal_the_contract_refuses_is_a_400_and_writes_nothing(screen, answers):
    call, log = screen
    before = log.read_text()
    assert call("goal", {"answers": answers})[0] == 400
    assert log.read_text() == before


@pytest.mark.parametrize("now", ["2030-03-03T06:00:00", "2030-03-03", "", None])
def test_clock_without_an_offset_is_a_400_and_writes_nothing(screen, now):
    call, log = screen
    before = log.read_text()
    assert call("clock", {"now": now})[0] == 400
    assert log.read_text() == before


@pytest.mark.parametrize("budget", ["0", "300", 12.5])
def test_written_records_meet_the_contract_with_one_budget_name(screen, budget):
    call, log = screen
    assert call("clock", {"now": "2030-03-03T06:00:00+01:00"})[0] == 200
    assert call("goal", {"answers": {"goal": "All on time", "unit_loss": "7", "budget": budget}})[0] == 200
    records = [json.loads(line) for line in log.read_text().splitlines()]
    for record in records:
        contracts.validate_record(record)
    (goal,) = [r for r in records if r["kind"] == "goal"]
    assert goal["budget_cap_eur"] == float(budget) and "budget_eur" not in goal
    assert json.loads(call("state")[1])["goal"]["budget_cap_eur"] == float(budget)


def test_a_statement_that_is_not_text_is_never_written(tmp_path):
    log = tmp_path / "log.jsonl"
    with pytest.raises(LookaheadError):
        append_records(log, [{"kind": "goal", "statement": 5}])
    assert not log.exists()


def test_a_producer_that_sends_both_budget_names_must_agree(tmp_path):
    log = tmp_path / "log.jsonl"
    with pytest.raises(LookaheadError):
        append_records(log, [{"kind": "goal", "budget_eur": 1, "budget_cap_eur": 2}])
    assert not log.exists()


@pytest.mark.parametrize("bad", [
    '{"kind": "goal", "budget_eur": NaN}',
    '{"kind": "plan", "lanes": [], "summary": {"cost": Infinity}}',
])
def test_a_non_finite_line_already_in_the_log_cannot_break_the_state(tmp_path, bad):
    log = tmp_path / "log.jsonl"
    log.write_text(bad + "\n")
    assert Lookahead(log).records == []


def test_a_goal_logged_under_the_old_budget_name_is_still_read(tmp_path):
    log = tmp_path / "log.jsonl"
    log.write_text('{"kind": "goal", "budget_eur": 300}\n')
    assert [r["budget_eur"] for r in Lookahead(log).records] == [300]
