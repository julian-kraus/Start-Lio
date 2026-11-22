"""Agent rules: the record kind, saving over HTTP, and the signer table. Every value here is invented."""

import json
import threading
import urllib.error
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from plan import contracts
from tracker import lookahead_api_rules, lookahead_rules
from tracker.lookahead import Lookahead, LookaheadError, append_records, fold
from tracker.web import App, make_handler

STATIC = Path(__file__).parents[1] / "tracker" / "lookahead_static"
DAY = date(2030, 3, 9)


def body(**change):
    return {
        "autonomy": {"quotes_and_holds": True, "check_ins": False, "reminders_and_calls": True,
                     "skip_hedge_with_substitute": False, "binding_orders": False},
        "hedge": {"min_share_pct": 3, "window_months": 6, "size_percentile": 90,
                  "unit_loss": {"mode": "refund", "value_eur": None}},
        "escalation": {"vacant_to_next_level": True, "remind_hours_before": 5, "digest_hours_before": None,
                       "channels": ["email"]},
        "based_on": None, **change}


class Clock:
    url = None

    def read(self):
        from datetime import datetime, timezone
        return datetime(2030, 3, 9, 23, 30, tzinfo=timezone.utc), False


@pytest.fixture
def la(tmp_path):
    return Lookahead(tmp_path / "log.jsonl", clock=Clock())


@pytest.fixture
def call(la, tmp_path):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(App(None, tmp_path, la)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def request(path, data=None, content_type="application/json"):
        req = urllib.request.Request(f"http://127.0.0.1:{httpd.server_address[1]}{path}",
                                     None if data is None else json.dumps(data).encode(),
                                     {"Content-Type": content_type})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    yield request
    httpd.shutdown()
    httpd.server_close()


def test_save_appends_and_a_reload_returns_the_latest(la, call):
    assert call("/api/lookahead/rules") == (200, {"rules": None, "saves": 0})
    status, first = call("/api/lookahead/rules", body())
    assert status == 200 and first["saves"] == 1
    contracts.validate_record(first["rules"])
    second = body(based_on=first["rules"]["id"], escalation={**body()["escalation"], "channels": ["sms"]})
    assert call("/api/lookahead/rules", second)[0] == 200
    reloaded = lookahead_rules.saved(Lookahead(la.path, clock=Clock()))
    assert reloaded["saves"] == 2 and reloaded["rules"]["escalation"]["channels"] == ["sms"]
    assert [json.loads(line)["kind"] for line in la.path.read_text().splitlines()] == ["rules", "rules"]
    assert la.state()["rules"]["id"] == reloaded["rules"]["id"]


def test_a_stale_page_does_not_overwrite(la, call):
    assert call("/api/lookahead/rules", body())[0] == 200
    assert call("/api/lookahead/rules", body())[0] == 409
    assert lookahead_rules.saved(la)["saves"] == 1


@pytest.mark.parametrize("section, key, value", [
    ("autonomy", "binding_orders", True), ("autonomy", "binding_orders", 0), ("autonomy", "check_ins", "yes"),
    ("autonomy", "approve_drafts", True), ("hedge", "window_months", 7), ("hedge", "min_share_pct", -1),
    ("hedge", "min_share_pct", float("nan")), ("hedge", "size_percentile", 101), ("hedge", "size_percentile", True),
    ("hedge", "unit_loss", {"mode": "custom", "value_eur": None}),
    ("hedge", "unit_loss", {"mode": "refund", "value_eur": 9}),
    ("escalation", "channels", ["fax"]), ("escalation", "remind_hours_before", "5"),
])
def test_bad_rules_are_refused(la, section, key, value):
    sent = body()
    sent[section] = {**sent[section], key: value}
    with pytest.raises(LookaheadError) as refused:
        lookahead_rules.save(la, sent)
    assert refused.value.status == 400 and not la.path.exists()


@pytest.mark.parametrize("change", [{"kind": "decision"}, {"producer": "agent"}, {"at": "yesterday"}, {"hedge": None}])
def test_the_page_cannot_set_other_fields(la, change):
    with pytest.raises(LookaheadError):
        lookahead_rules.save(la, body(**change))
    assert not la.path.exists()


def test_a_custom_loss_carries_its_source(la):
    sent = body()
    sent["hedge"]["unit_loss"] = {"mode": "custom", "value_eur": 7}
    record = lookahead_rules.save(la, sent)["rules"]
    assert record["hedge"]["unit_loss"]["value_eur"] == 7 and record["source"] == lookahead_rules.SOURCE


@pytest.mark.parametrize("producer", ["agent", "agent:hedger", "monitor"])
def test_an_agent_cannot_set_its_own_rules(la, producer):
    kept = lookahead_rules.save(la, body())["rules"]
    append_records(la.path, [{**{k: v for k, v in kept.items() if k not in ("id", "producer")}, "producer": producer}])
    assert lookahead_rules.saved(Lookahead(la.path, clock=Clock()))["rules"]["id"] == kept["id"]


def test_fold_keeps_the_latest_rules():
    assert fold([{"kind": "rules", "id": 1}, {"kind": "rules", "id": 2}])["rules"]["id"] == 2


def test_a_post_from_a_form_is_refused(la, call):
    assert call("/api/lookahead/rules", body(), "text/plain")[0] == 415
    assert not la.path.exists()


def test_signers_without_a_database_say_so(call):
    assert call("/api/lookahead/rules/signers") == (200, lookahead_rules.unavailable("database"))


def test_the_day_follows_the_team_time_zone(la):
    la.append([{"kind": "launch", "timezone": "Asia/Taipei"}])
    assert lookahead_api_rules.team_day(la) == date(2030, 3, 10)


@pytest.fixture
def world():
    pytest.importorskip("sourcing.approvals", reason="the approval reader is on main, not yet on the parent branch")
    import approvals_world
    return approvals_world.world


def test_signers_come_from_the_policy_and_chart_facts(world):
    out = lookahead_rules.signers(world(), DAY)
    assert out["available"] and out["policy"]["name"] == "AP-1"
    assert all(row["limit"]["fact_ids"] for row in out["rows"])
    assert out["rows"][0]["limit"]["value"] == 700
    seats = [seat for row in out["rows"] for seat in row["seats"]]
    assert "Cleo Cole" in {seat.get("name") for seat in seats}
    assert any(seat["state"] == "vacant" for seat in seats)
    assert all(("name" in seat) == (seat["state"] == "held") for seat in seats)


def test_signers_follow_the_facts(world):
    from approvals_world import POLICY
    out = lookahead_rules.signers(world(POLICY.replace("< 700\n", "< 1,500\n")), DAY)
    assert out["rows"][0]["limit"]["value"] == 1500


def test_no_name_when_the_chart_is_in_doubt(world):
    from approvals_world import CHART_V2, POLICY, SEATS_V2, changed
    other = changed(SEATS_V2, "Area Manager Paper & Board", holder="Kim King")
    out = lookahead_rules.signers(world(POLICY, (CHART_V2, SEATS_V2), (CHART_V2, other)), date(2030, 3, 11))
    assert out["problems"] and not any("name" in seat for row in out["rows"] for seat in row["seats"])


def test_no_policy_is_said(world):
    assert lookahead_rules.signers([], DAY)["reason"] == "policy"


def test_every_label_has_both_languages():
    strings = json.loads((STATIC / "strings" / "rules.json").read_text())
    assert strings and all(key.startswith("rules.") and row["en"] and row["zh"] for key, row in strings.items())
