"""HTTP routes of the Lookahead screen, mixed into the handler of tracker/web.py.

GET  /lookahead                              the timeline page
GET  /lookahead/static/<file>                its scripts and styles
GET  /api/lookahead/state                    folded state: launch, clock, goal, plan, events, decisions, promises, suppliers
GET  /api/lookahead/clock                    the clock alone, polled by open pages in live mode
GET  /api/lookahead/events/<id>              one world event with its drill-down
GET  /api/lookahead/stream                   SSE, one message per appended record (resumes via Last-Event-ID)
POST /api/lookahead/goal                     {answers: {id: value}} from the goal interview
GET  /api/lookahead/decisions/<id>           the decision card: routed signer, score drops, end state
POST /api/lookahead/decisions/<id>/accept    {approver}; only the routed signer; live mode writes plan.decisions and sends
                                             the attached drafts (as the mail send mode says)
POST /api/lookahead/decisions/<id>/reject    {approver, reason}; writes the reason, sends nothing
POST /api/lookahead/advance | reset          rehearse mode only: next beat / start over
POST /api/lookahead/clock                    {now}; rehearse mode, or forward on a running scenario clock
POST /api/lookahead/clock/next               live scenario: jump to the launch record's next demo time

Every POST must be application/json, and a browser's must come from the page itself (see `refuse_foreign_post`).
Open the screen by IP address or as localhost: under any other host name its buttons are refused.
"""

from __future__ import annotations

import ipaddress
import json
import queue
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import lookahead_decisions as decisions
from .lookahead import Lookahead, LookaheadError
from .lookahead_clock import parse_time

PAGE = Path(__file__).with_name("lookahead_page.html")
STATIC = Path(__file__).with_name("lookahead_static")
STATIC_FILE = re.compile(r"/lookahead/static/([a-z0-9_]+\.(js|css))")
STATIC_TYPES = {"js": "text/javascript; charset=utf-8", "css": "text/css; charset=utf-8"}
MAX_BODY = 64 * 1024
DECISION = r"/api/lookahead/decisions/([^/]+)"  # ids are URL-encoded: the monitor's contain ':'
HEARTBEAT = 15


def dumps(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode()


def refuse_foreign_post(headers) -> None:
    """Keep other web pages from using the operator's browser to press the screen's buttons.

    JSON cannot be sent across origins without a preflight, which this server does not answer. An Origin, which
    browsers add to every POST, must equal the Host asked for, and that host must be an IP address or localhost:
    any other name can be pointed at this machine by whoever owns it.
    """
    if headers.get_content_type() != "application/json":
        raise LookaheadError(415, "Content-Type must be application/json")
    origins = headers.get_all("Origin")
    if origins is None:
        return  # not a browser
    host = headers.get("Host", "").lower()
    if [o.lower() for o in origins] != [f"http://{host}"] or not is_local_name(host):
        raise LookaheadError(403, "POST is accepted only from this screen, opened by IP address or as localhost")


def is_local_name(host: str) -> bool:
    try:
        name = urlsplit(f"//{host}").hostname or ""
        if name != "localhost":
            ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


def lookahead_goal(la: Lookahead, body: dict, *, client=None, stores=None) -> dict:
    """Run the goal interview on the typed answers, then read it back or confirm it.

    Without an author the answers are read into a draft and shown back with its open points, and nothing is
    stored. With an author the confirmed goal is stored in plan.goals (who set it and when) and the records that
    put it on the timeline are appended. Both steps run the same reading, so the person confirms what was shown.
    """
    answers = body.get("answers")
    if not isinstance(answers, dict) or not answers:
        raise LookaheadError(400, "answers must be a non-empty object")
    author = body.get("author")
    launch = la.state()["launch"] or {}
    known = {q["id"] for q in launch.get("goal_questions", [])}
    unknown = set(answers) - known
    if unknown:
        raise LookaheadError(400, f"unknown answers: {sorted(unknown)}")

    from agents import interview as iv, interview_store as store

    stores = stores or decisions.Stores()
    client = client or _goal_client()
    try:
        with stores.plan() as conn:
            draft = iv.interview(client, answers, conn=conn, editions=launch.get("editions") or (),
                                 inputs=launch.get("goal_inputs"), zone=launch.get("timezone"),
                                 product=launch.get("product"), deadline=launch.get("deadline"))
            shown = draft.show()
            if author is None:  # no author yet: a read-back. An empty author is a confirm that store.confirm refuses.
                return {"ok": True, "confirmed": False, "ready": draft.ready, "show": shown}
            goal = store.confirm(conn, draft, author)
    except iv.InterviewError as exc:
        raise LookaheadError(400, str(exc)) from None
    la.append(store.records(goal))
    la.after_human_step()
    return {"ok": True, "confirmed": True, "show": shown}


def _goal_client():
    """The live agent adapter, built only when a goal is actually interviewed (it needs the configured key)."""
    from agents.llm import AnthropicClient

    try:
        return AnthropicClient()
    except RuntimeError as exc:
        raise LookaheadError(503, str(exc)) from None


def lookahead_accept(la: Lookahead, decision_id: str, body: dict, stores=None) -> dict:
    """Accept through the decision card: only the routed signer, plan row, drafts sent as the mail mode says."""
    return decisions.accept(la, decision_id, body, stores)


def lookahead_reject(la: Lookahead, decision_id: str, body: dict, stores=None) -> dict:
    return decisions.reject(la, decision_id, body, stores)


class LookaheadRoutes:
    """Handler methods; the host class provides `lookahead` and `_send(status, body, ctype)`."""

    lookahead: Lookahead | None = None
    decision_stores = None  # kb, plan and mail for the decision card; None reads the configured databases
    goal_client = None  # LLM adapter for the goal interview; None builds the live Anthropic client

    def _lookahead_file(self, path: str):
        if path == "/lookahead":
            return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        m = STATIC_FILE.fullmatch(path)
        if not m or not (STATIC / m.group(1)).is_file():
            raise LookaheadError(404, "not found")
        self._send(200, (STATIC / m.group(1)).read_bytes(), STATIC_TYPES[m.group(2)])

    def _need_lookahead(self) -> Lookahead:
        if not self.lookahead:
            raise LookaheadError(404, "lookahead is not enabled")
        return self.lookahead

    def lookahead_get(self, path: str, params: dict):
        try:
            if path == "/lookahead" or path.startswith("/lookahead/static/"):
                return self._lookahead_file(path)
            la = self._need_lookahead()
            if path == "/api/lookahead/state":
                return self._send(200, dumps(la.state()))
            if path == "/api/lookahead/clock":
                return self._send(200, dumps(la.live_clock() if la.mode == "live" else la.state()["clock"]))
            if m := re.fullmatch(r"/api/lookahead/events/(\d+)", path):
                row = la.event_detail(int(m.group(1)))
                return self._send(200 if row else 404, dumps(row or {"error": "not found"}))
            if m := re.fullmatch(DECISION, path):
                return self._send(200, dumps(decisions.card(la, unquote(m.group(1)), self.decision_stores)))
            if path == "/api/lookahead/stream":
                return self._lookahead_stream(la, params)
            self._send(404, dumps({"error": "not found"}))
        except LookaheadError as exc:
            self._send(exc.status, dumps({"error": str(exc)}))

    def lookahead_post(self, path: str):
        try:
            la = self._need_lookahead()
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise LookaheadError(413, "body too large")
            raw = self.rfile.read(length) if length else b"{}"
            refuse_foreign_post(self.headers)  # after the read: closing on an unread body can reset the connection
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                raise LookaheadError(400, "body must be JSON") from None
            if not isinstance(body, dict):
                raise LookaheadError(400, "body must be a JSON object")
            if path == "/api/lookahead/goal":
                return self._send(200, dumps(lookahead_goal(la, body, client=self.goal_client,
                                                             stores=self.decision_stores)))
            if m := re.fullmatch(DECISION + "/accept", path):
                return self._send(200, dumps(lookahead_accept(la, unquote(m.group(1)), body, self.decision_stores)))
            if m := re.fullmatch(DECISION + "/reject", path):
                return self._send(200, dumps(lookahead_reject(la, unquote(m.group(1)), body, self.decision_stores)))
            if path == "/api/lookahead/advance":
                return self._send(200, dumps(la.advance()))
            if path == "/api/lookahead/reset":
                la.reset()
                return self._send(200, dumps({"ok": True}))
            if path == "/api/lookahead/clock":
                now = parse_time(body.get("now") or "").isoformat()
                if la.mode == "live":
                    now = la.move_clock(now)
                else:
                    la.append([{"kind": "clock", "now": now}])
                return self._send(200, dumps({"ok": True, "now": now}))
            if path == "/api/lookahead/clock/next":
                if la.mode != "live":
                    raise LookaheadError(409, "a rehearsal moves its clock with the next beat")
                return self._send(200, dumps({"ok": True, "now": la.move_clock()}))
            self._send(404, dumps({"error": "not found"}))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except LookaheadError as exc:
            self._send(exc.status, dumps({"error": str(exc)}))

    def _lookahead_stream(self, la: Lookahead, params: dict):
        q = la.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.wfile.write(b"retry: 2000\n\n")
            last = int(self.headers.get("Last-Event-ID") or params.get("after") or 0)
            for rec in la.since(last):
                last = self._emit_record(rec)
            self.wfile.flush()
            while True:
                try:
                    rec = q.get(timeout=HEARTBEAT)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if rec["kind"] == "reset" or rec["id"] > last:
                    last = self._emit_record(rec)
                    self.wfile.flush()
        finally:
            la.unsubscribe(q)

    def _emit_record(self, rec: dict) -> int:
        self.wfile.write(b"id: %d\nevent: record\ndata: " % rec["id"] + dumps(rec) + b"\n\n")
        return rec["id"]
