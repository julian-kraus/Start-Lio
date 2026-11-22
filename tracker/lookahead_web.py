"""HTTP routes of the Lookahead screen, mixed into the handler of tracker/web.py.

GET  /lookahead                              the timeline page
GET  /lookahead/static/<file>                its scripts and styles
GET  /api/lookahead/state                    folded state: launch, clock, goal, plan, events, decisions, promises, suppliers
GET  /api/lookahead/clock                    the clock alone, polled by open pages in live mode
GET  /api/lookahead/events/<id>              one world event with its drill-down
GET  /api/lookahead/stream                   SSE, one message per appended record (resumes via Last-Event-ID)
POST /api/lookahead/goal                     {answers: {id: value}} from the goal interview
POST /api/lookahead/decisions/<id>/accept    {approver}; live mode approves and sends the decision's mail drafts
POST /api/lookahead/advance | reset          rehearse mode only: next beat / start over
POST /api/lookahead/clock                    {now}; rehearse mode, or forward on a running scenario clock
POST /api/lookahead/clock/next               live scenario: jump to the launch record's next demo time
"""

from __future__ import annotations

import json
import queue
import re
from pathlib import Path

from .lookahead import Lookahead, LookaheadError
from .lookahead_clock import parse_time

PAGE = Path(__file__).with_name("lookahead_page.html")
STATIC = Path(__file__).with_name("lookahead_static")
STATIC_FILE = re.compile(r"/lookahead/static/([a-z0-9_]+\.(js|css))")
STATIC_TYPES = {"js": "text/javascript; charset=utf-8", "css": "text/css; charset=utf-8"}
MAX_BODY = 64 * 1024
HEARTBEAT = 15


def dumps(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode()


def lookahead_goal(la: Lookahead, body: dict) -> dict:
    answers = body.get("answers")
    if not isinstance(answers, dict) or not answers:
        raise LookaheadError(400, "answers must be a non-empty object")
    st = la.state()
    questions = (st["launch"] or {}).get("goal_questions", [])
    known = {q["id"] for q in questions}
    unknown = set(answers) - known
    if unknown:
        raise LookaheadError(400, f"unknown answers: {sorted(unknown)}")
    now = la.now()
    rec = {"kind": "goal", "at": now, "answers": answers,
           "questions": [{"id": q["id"], "q": q["q"], "a": answers.get(q["id"]), "unit": q.get("unit")} for q in questions]}
    for key in ("unit_loss", "budget"):
        if key in answers:
            try:
                rec[key + "_eur"] = float(answers[key])
            except (TypeError, ValueError):
                raise LookaheadError(400, f"{key} must be a number") from None
    rec["statement"] = answers.get("goal", "")
    la.append([rec, {"kind": "event", "type": "goal", "at": now, "severity": "info",
                     "title": "Goal set: " + (rec["statement"] or "updated"),
                     "detail": {"happened": "Goal interview answered.",
                                "reasoning": [{"text": q["q"], "value": str(q["a"]) + (" " + q["unit"] if q.get("unit") else "")}
                                              for q in rec["questions"] if q["a"] not in (None, "")],
                                "autonomy": {"mode": "none", "text": "Set by you. Every later change shows up here."}}}])
    la.after_human_step()
    return {"ok": True}


def lookahead_accept(la: Lookahead, decision_id: str, body: dict) -> dict:
    approver = str(body.get("approver") or "").strip()
    if not approver or approver.lower().startswith(("agent", "mail:", "system")):
        raise LookaheadError(400, "approver must name a person")
    d = la.decision(decision_id)
    if not d:
        raise LookaheadError(404, "unknown decision")
    if d.get("status") != "open":
        raise LookaheadError(409, f"decision is {d.get('status')}")
    now = la.now()  # before anything is sent: without a clock the decision could not be recorded
    sent = []
    if la.mode == "live":
        # Live: approve and send exactly the drafts the agent attached to this decision.
        from mail import drafts as mail_drafts
        from mail.tools import MailTools
        tools = MailTools(actor=approver)
        signer = d.get("signer") or {}
        for draft_id in d.get("draft_ids", []):
            try:
                ok = mail_drafts.approve(tools.conn, draft_id, approver, role=signer.get("role"),
                                         policy_ref=signer.get("rule"))
                tools.conn.commit()
            except mail_drafts.DraftError as exc:
                tools.conn.rollback()
                raise LookaheadError(409, f"draft {draft_id}: {exc.code}") from exc
            res = tools.send_approved(draft_id, ok["approval_id"])
            if not res.get("ok"):
                raise LookaheadError(502, f"draft {draft_id} not sent: {res.get('error') or res}")
            sent.append(draft_id)
    la.record_decision(decision_id, "accepted", approver, now)
    la.after_human_step()
    return {"ok": True, "sent": sent}


class LookaheadRoutes:
    """Handler methods; the host class provides `lookahead` and `_send(status, body, ctype)`."""

    lookahead: Lookahead | None = None

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
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                raise LookaheadError(400, "body must be JSON") from None
            if not isinstance(body, dict):
                raise LookaheadError(400, "body must be a JSON object")
            if path == "/api/lookahead/goal":
                return self._send(200, dumps(lookahead_goal(la, body)))
            if m := re.fullmatch(r"/api/lookahead/decisions/([\w.-]+)/accept", path):
                return self._send(200, dumps(lookahead_accept(la, m.group(1), body)))
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
