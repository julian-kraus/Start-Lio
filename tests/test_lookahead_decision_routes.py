"""The screen's decision routes go through the decision card: routed signer, plan row, reject. Over HTTP, on a
throwaway Postgres, mail in dry_run. Everything is invented."""

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from test_lookahead_decisions import SIGNER, count, decision, draft, row, screen, stores  # noqa: F401
from tracker import lookahead_web
from tracker.web import App, make_handler


@pytest.fixture
def serve(tmp_path, stores, monkeypatch):  # noqa: F811
    monkeypatch.setattr(lookahead_web.LookaheadRoutes, "decision_stores", stores)
    running = []

    def start(la):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(App(None, tmp_path, la)))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        running.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    yield start
    for httpd in running:
        httpd.shutdown()
        httpd.server_close()


def call(base, path, body=None):
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"},
                                 method="GET" if body is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_accept_route_lets_only_the_routed_signer_accept(tmp_path, serve, stores):  # noqa: F811
    la = screen(tmp_path, decision(draft_ids=[draft(stores)]))
    base = serve(la)
    status, body = call(base, "/api/lookahead/decisions/D/accept", {"approver": "Mallory Producer"})
    assert status == 403 and SIGNER in body["error"]
    assert row(stores) is None and count(stores, "sends") == 0
    status, body = call(base, "/api/lookahead/decisions/D/accept", {"approver": SIGNER})
    assert status == 200 and len(body["sent"]) == 1
    saved = row(stores)
    assert (saved["status"], saved["decided_by"], saved["signer"]["name"]) == ("accepted", SIGNER, SIGNER)
    states = stores.db["mail"].execute("SELECT state FROM sends").fetchall()
    assert [s["state"] for s in states] == ["dry_run"]


def test_reject_route_records_the_reason_and_sends_nothing(tmp_path, serve, stores):  # noqa: F811
    la = screen(tmp_path, decision(draft_ids=[draft(stores)]))
    base = serve(la)
    assert call(base, "/api/lookahead/decisions/D/reject", {"approver": SIGNER})[0] == 400
    status, _ = call(base, "/api/lookahead/decisions/D/reject", {"approver": SIGNER, "reason": "Too expensive"})
    assert status == 200
    saved = row(stores)
    assert (saved["status"], saved["note"]) == ("rejected", "Too expensive")
    assert count(stores, "sends") == 0 and la.decision("D")["status"] == "rejected"


def test_card_route_shows_the_routed_signer_not_the_producers(tmp_path, serve, stores):  # noqa: F811
    base = serve(screen(tmp_path, decision()))
    status, card = call(base, "/api/lookahead/decisions/D")
    assert status == 200 and card["signer"]["name"] == SIGNER


def test_monitor_decision_ids_reach_the_routes(tmp_path, serve, stores):  # noqa: F811
    monitor_id = "replan:P-1:2030-03-11T06:00:00+00:00"
    la = screen(tmp_path, decision(decision_id=monitor_id, draft_ids=[draft(stores)]))
    base = serve(la)
    quoted = urllib.parse.quote(monitor_id, safe="")
    assert call(base, f"/api/lookahead/decisions/{quoted}")[0] == 200
    status, _ = call(base, f"/api/lookahead/decisions/{quoted}/accept", {"approver": SIGNER})
    assert status == 200 and la.decision(monitor_id)["status"] == "accepted"
