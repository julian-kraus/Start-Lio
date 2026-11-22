"""Lookahead POST routes take JSON from the server's own origin only. The route effects are recorded, not run:
no mail, no database."""

import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from tracker import lookahead_web
from tracker.lookahead import Lookahead
from tracker.web import App, make_handler

JSON = "application/json"
ROUTES = ["goal", "decisions/D-1/accept", "advance", "reset", "clock", "clock/next"]
BODY = json.dumps({"answers": {"goal": "x"}, "approver": "Test Person", "now": "2030-03-03T06:00:00+01:00"}).encode()


@pytest.fixture
def screen(tmp_path, monkeypatch):
    la = Lookahead(tmp_path / "log.jsonl")
    reached = []

    def record(*_args):
        reached.append(1)
        return {"ok": True}

    for name in ("lookahead_goal", "lookahead_accept"):
        monkeypatch.setattr(lookahead_web, name, record)
    for name in ("advance", "reset", "move_clock"):
        monkeypatch.setattr(la, name, record)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(App(None, tmp_path, la)))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True).start()
    yield server.server_address[1], reached
    server.shutdown()
    server.server_close()


def post(port, route, *headers, body=BODY):
    """POST with exactly these headers, repeats included. `{port}` in a value is the server's port."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    skip_host = any(name == "Host" for name, _ in headers)
    conn.putrequest("POST", f"/api/lookahead/{route}", skip_host=skip_host)
    for name, value in (*headers, ("Content-Length", str(len(body)))):
        conn.putheader(name, value.replace("{port}", str(port)))
    conn.endheaders(body)
    res = conn.getresponse()
    status, answer = res.status, json.loads(res.read())
    conn.close()
    return status, answer


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("ctype", [None, "", "text/plain", "application/x-www-form-urlencoded",
                                   "multipart/form-data; boundary=b", "text/plain; x=application/json"])
def test_post_without_json_content_type_is_refused(screen, route, ctype):
    port, reached = screen
    status, answer = post(port, route, *([] if ctype is None else [("Content-Type", ctype)]))
    assert status == 415 and answer["error"]
    assert not reached


def test_empty_post_without_content_type_is_refused(screen):
    port, reached = screen
    assert post(port, "reset", body=b"")[0] == 415
    assert not reached


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("headers", [
    [("Origin", "https://other.example")],
    [("Origin", "null")],
    [("Origin", "")],
    [("Origin", "http://127.0.0.1")],  # another port
    [("Origin", "https://127.0.0.1:{port}")],  # another scheme
    [("Origin", "http://localhost:{port}")],  # another host
    [("Origin", "http://127.0.0.1:{port}"), ("Origin", "https://other.example")],
    [("Host", "rebind.example:{port}"), ("Origin", "http://rebind.example:{port}")],  # a name pointed at this machine
    [("Host", "localhost.other.example:{port}"), ("Origin", "http://localhost.other.example:{port}")],
    [("Host", "[broken"), ("Origin", "http://[broken")],
])
def test_post_from_another_origin_is_refused(screen, route, headers):
    port, reached = screen
    status, answer = post(port, route, ("Content-Type", JSON), *headers)
    assert status == 403 and answer["error"]
    assert not reached


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("headers", [
    [],  # a local client that is not a browser
    [("Origin", "http://127.0.0.1:{port}")],
    [("Host", "localhost:{port}"), ("Origin", "http://localhost:{port}")],
    [("Host", "[::1]:{port}"), ("Origin", "http://[::1]:{port}")],
])
def test_own_page_and_local_clients_still_work(screen, route, headers):
    port, reached = screen
    assert post(port, route, ("Content-Type", f"{JSON}; charset=utf-8"), *headers)[0] == 200
    assert reached == [1]


def test_the_page_posts_json():
    assert '"Content-Type": "application/json"' in (lookahead_web.STATIC / "core.js").read_text()
