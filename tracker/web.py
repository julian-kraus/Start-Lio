"""Local web UI for change events: python -m tracker.web [--port 8787].

GET /                    the feed page
GET /api/status          last poll and totals
GET /api/events          newest first; ?before=<id>&limit=&kind=&q=
GET /api/events/<id>     one event with its full diff
GET /api/stream          Server-Sent Events, one message per new event (resumes via Last-Event-ID)
GET /api/blob/<sha256>   raw content of a stored file version
GET /api/mail            supplier mail: threads, unmatched inbound messages, counts (empty if no mail schema)
GET /api/mail/threads/<uuid>  one thread with its messages, attachments (names only) and drafts

Lookahead (launch timeline, see tracker/lookahead.py):
GET  /lookahead                              the timeline page
GET  /api/lookahead/state                    folded state: launch, clock, goal, plan, events, decisions, promises, suppliers
GET  /api/lookahead/events/<id>              one world event with its drill-down
GET  /api/lookahead/stream                   SSE, one message per appended record (resumes via Last-Event-ID)
POST /api/lookahead/goal                     {answers: {id: value}} from the goal interview
POST /api/lookahead/decisions/<id>/accept    {approver}; live mode approves and sends the decision's mail drafts
POST /api/lookahead/advance | reset          rehearse mode only: next beat / start over
POST /api/lookahead/clock                    {now}; rehearse mode only
"""

from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import os
import queue
import re
import threading
import time
import uuid
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import psycopg

from . import db
from .config import PROJECT_ROOT, database_url, load_env
from .lookahead import Lookahead, LookaheadError
from .store import BlobStore

log = logging.getLogger("tracker.web")
PAGE = Path(__file__).with_name("web_page.html")
LOOKAHEAD_PAGE = Path(__file__).with_name("lookahead_page.html")
MAX_BODY = 64 * 1024
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
LIST_COLUMNS = "id, at, kind, path, old_path, old_sha, new_sha, summary"
HEARTBEAT = 15
UUID_RE = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# Never select messages.raw (full RFC 822 bytes): the page only needs headers and text.
MAIL_MESSAGE_COLUMNS = ("id, message_id, thread_id, direction, kind, from_addr, from_name, to_addrs, subject, "
                        "date_header, received_at, supplier_id, trust, lang, flags, body_text, body_en")


def _json_default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    raise TypeError(f"not serializable: {type(value).__name__}")


def dumps(obj) -> bytes:
    return json.dumps(obj, default=_json_default, ensure_ascii=False).encode()


class Hub:
    """One LISTEN connection for the whole server, fanned out to every open SSE client."""

    def __init__(self, url: str):
        self.url = url
        self.clients: set[queue.Queue] = set()
        self.lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self.lock:
            self.clients.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            self.clients.discard(q)

    def run(self) -> None:
        while True:
            try:
                with db.connect(self.url, autocommit=True) as conn:
                    conn.execute(f"LISTEN {db.NOTIFY_CHANNEL}")
                    last = conn.execute("SELECT coalesce(max(id), 0) AS m FROM events").fetchone()["m"]
                    log.info("listening for events after #%d", last)
                    while True:
                        # notifies() holds the connection lock, so no queries inside the loop:
                        # wait for a wake-up, then read everything newer than the last broadcast.
                        for _ in conn.notifies(timeout=30, stop_after=1):
                            pass
                        rows = conn.execute(
                            f"SELECT {LIST_COLUMNS} FROM events WHERE id > %s ORDER BY id", (last,)
                        ).fetchall()
                        for row in rows:
                            self._broadcast(row)
                            last = row["id"]
            except psycopg.Error as exc:
                log.warning("listener lost the database (%s), reconnecting", exc)
                time.sleep(3)

    def _broadcast(self, row: dict) -> None:
        with self.lock:
            for q in list(self.clients):
                try:
                    q.put_nowait(row)
                except queue.Full:  # a stalled client; it will resume from Last-Event-ID
                    self.clients.discard(q)


class App:
    def __init__(self, url: str | None, data_dir: Path, lookahead: Lookahead | None = None):
        self.url = url
        self.store = BlobStore(data_dir)
        self.hub = Hub(url) if url else None
        self.local = threading.local()
        self.lookahead = lookahead

    def conn(self) -> psycopg.Connection:
        if not self.url:
            raise psycopg.OperationalError("no database configured (set DATABASE_URL)")
        conn = getattr(self.local, "conn", None)
        if conn is None or conn.closed or conn.broken:
            conn = db.connect(self.url, autocommit=True)
            self.local.conn = conn
        return conn

    def status(self) -> dict:
        c = self.conn()
        last = c.execute(
            "SELECT id, finished_at, ok, error, n_files, failed FROM polls WHERE note IS NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        counts = c.execute(
            "SELECT (SELECT count(*) FROM file_versions WHERE superseded_at IS NULL) AS files, "
            "(SELECT count(*) FROM file_versions) AS versions, (SELECT count(*) FROM events) AS events, "
            "(SELECT min(finished_at) FROM polls WHERE ok AND baseline) AS baseline_at, "
            "(SELECT max(finished_at) FROM polls WHERE ok) AS last_ok"
        ).fetchone()
        return {"last_poll": last, **counts}

    def events(self, params: dict) -> list[dict]:
        clauses, args = [], []
        if params.get("before"):
            clauses.append("id < %s")
            args.append(int(params["before"]))
        if params.get("after"):
            clauses.append("id > %s")
            args.append(int(params["after"]))
        if params.get("kind"):
            clauses.append("kind = ANY(%s)")
            args.append(params["kind"].split(","))
        if params.get("q"):
            clauses.append("(path ILIKE %s OR summary ILIKE %s)")
            like = "%" + params["q"].replace("%", r"\%").replace("_", r"\_") + "%"
            args += [like, like]
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit = min(int(params.get("limit", 50)), 500)
        return self.conn().execute(
            f"SELECT {LIST_COLUMNS} FROM events {where} ORDER BY id DESC LIMIT %s", (*args, limit)
        ).fetchall()

    def event(self, event_id: int) -> dict | None:
        return self.conn().execute("SELECT * FROM events WHERE id = %s", (event_id,)).fetchone()

    def _has_mail(self, c) -> bool:
        return c.execute("SELECT to_regclass('mail.threads') IS NOT NULL AS ok").fetchone()["ok"]

    def mail(self) -> dict:
        c = self.conn()
        if not self._has_mail(c):
            return {"available": False}
        threads = c.execute(
            """SELECT t.thread_id, t.tag, t.supplier_id, t.subject, t.status, t.po_numbers, t.last_message_at,
                      (SELECT count(*) FROM mail.messages m WHERE m.thread_id = t.thread_id) AS n_messages,
                      (SELECT count(*) FROM mail.drafts d WHERE d.thread_id = t.thread_id AND d.status = 'draft') AS open_drafts,
                      (SELECT m.kind FROM mail.messages m WHERE m.thread_id = t.thread_id AND m.direction = 'in'
                        ORDER BY m.received_at DESC LIMIT 1) AS last_inbound_kind
               FROM mail.threads t ORDER BY t.last_message_at DESC NULLS LAST, t.created_at DESC LIMIT 200"""
        ).fetchall()
        unmatched = c.execute(
            f"""SELECT {MAIL_MESSAGE_COLUMNS} FROM mail.messages
                WHERE thread_id IS NULL AND direction = 'in' ORDER BY received_at DESC LIMIT 100"""
        ).fetchall()
        counts = c.execute(
            """SELECT (SELECT count(*) FROM mail.messages) AS messages,
                      (SELECT count(*) FROM mail.messages WHERE direction = 'in') AS inbound,
                      (SELECT count(*) FROM mail.sends WHERE state = 'sent') AS sent,
                      (SELECT max(received_at) FROM mail.messages WHERE direction = 'in') AS last_inbound"""
        ).fetchone()
        return {"available": True, "threads": threads, "unmatched": unmatched, **counts}

    def mail_thread(self, thread_id: str) -> dict | None:
        c = self.conn()
        if not self._has_mail(c):
            return None
        thread = c.execute("SELECT * FROM mail.threads WHERE thread_id = %s", (thread_id,)).fetchone()
        if not thread:
            return None
        thread["messages"] = c.execute(
            f"""SELECT {MAIL_MESSAGE_COLUMNS},
                       coalesce((SELECT json_agg(json_build_object('filename', a.filename, 'bytes', a.bytes,
                                 'content_type', a.content_type) ORDER BY a.id)
                                 FROM mail.attachments a WHERE a.message_pk = m.id), '[]') AS attachments
                FROM mail.messages m WHERE m.thread_id = %s ORDER BY m.received_at""", (thread_id,)
        ).fetchall()
        thread["drafts"] = c.execute(
            """SELECT draft_id, status, subject, lang, to_addrs, body_local, body_en, created_by, created_at, updated_at
               FROM mail.drafts WHERE thread_id = %s ORDER BY created_at""", (thread_id,)
        ).fetchall()
        return thread


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
    rec = {"kind": "goal", "at": st["clock"]["now"], "answers": answers,
           "questions": [{"id": q["id"], "q": q["q"], "a": answers.get(q["id"]), "unit": q.get("unit")} for q in questions]}
    for key in ("unit_loss", "budget"):
        if key in answers:
            try:
                rec[key + "_eur"] = float(answers[key])
            except (TypeError, ValueError):
                raise LookaheadError(400, f"{key} must be a number") from None
    rec["statement"] = answers.get("goal", "")
    la.append([rec, {"kind": "event", "type": "goal", "at": st["clock"]["now"], "severity": "info",
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
    sent = []
    if la.state()["mode"] == "live":
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
    la.record_decision(decision_id, "accepted", approver, la.state()["clock"]["now"])
    la.after_human_step()
    return {"ok": True, "sent": sent}


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "tracker-web"

        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

        def _send(self, status: int, body: bytes, ctype: str = "application/json", extra: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlsplit(self.path)
            params = {k: v[-1] for k, v in parse_qs(url.query).items()}
            try:
                if url.path == "/":
                    return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
                if url.path == "/api/status":
                    return self._send(200, dumps(app.status()))
                if url.path == "/api/events":
                    return self._send(200, dumps(app.events(params)))
                if m := re.fullmatch(r"/api/events/(\d+)", url.path):
                    row = app.event(int(m.group(1)))
                    return self._send(200 if row else 404, dumps(row or {"error": "not found"}))
                if m := re.fullmatch(r"/api/blob/([0-9a-f]{64})", url.path):
                    return self._blob(m.group(1), params.get("name", ""))
                if url.path == "/api/mail":
                    return self._send(200, dumps(app.mail()))
                if m := re.fullmatch(rf"/api/mail/threads/({UUID_RE})", url.path):
                    row = app.mail_thread(m.group(1))
                    return self._send(200 if row else 404, dumps(row or {"error": "not found"}))
                if url.path == "/api/stream":
                    return self._stream(params)
                if url.path == "/lookahead" or url.path.startswith("/api/lookahead/"):
                    return self._lookahead_get(url.path, params)
                self._send(404, dumps({"error": "not found"}))
            except (BrokenPipeError, ConnectionResetError):
                pass
            except (ValueError, psycopg.Error) as exc:
                self._send(400 if isinstance(exc, ValueError) else 503, dumps({"error": str(exc)}))

        def _need_lookahead(self) -> Lookahead:
            if not app.lookahead:
                raise LookaheadError(404, "lookahead is not enabled")
            return app.lookahead

        def _lookahead_get(self, path: str, params: dict):
            try:
                if path == "/lookahead":
                    return self._send(200, LOOKAHEAD_PAGE.read_bytes(), "text/html; charset=utf-8")
                la = self._need_lookahead()
                if path == "/api/lookahead/state":
                    return self._send(200, dumps(la.state()))
                if m := re.fullmatch(r"/api/lookahead/events/(\d+)", path):
                    row = la.event_detail(int(m.group(1)))
                    return self._send(200 if row else 404, dumps(row or {"error": "not found"}))
                if path == "/api/lookahead/stream":
                    return self._lookahead_stream(la, params)
                self._send(404, dumps({"error": "not found"}))
            except LookaheadError as exc:
                self._send(exc.status, dumps({"error": str(exc)}))

        def do_POST(self):
            url = urlsplit(self.path)
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
                if url.path == "/api/lookahead/goal":
                    return self._send(200, dumps(lookahead_goal(la, body)))
                if m := re.fullmatch(r"/api/lookahead/decisions/([\w.-]+)/accept", url.path):
                    return self._send(200, dumps(lookahead_accept(la, m.group(1), body)))
                if url.path == "/api/lookahead/advance":
                    return self._send(200, dumps(la.advance()))
                if url.path == "/api/lookahead/reset":
                    la.reset()
                    return self._send(200, dumps({"ok": True}))
                if url.path == "/api/lookahead/clock":
                    if la.state()["mode"] != "rehearse":
                        raise LookaheadError(409, "the clock can only be set in rehearse mode")
                    now = str(body.get("now") or "")
                    try:
                        datetime.fromisoformat(now)
                    except ValueError:
                        raise LookaheadError(400, "now must be an ISO timestamp") from None
                    la.append([{"kind": "clock", "now": now}])
                    return self._send(200, dumps({"ok": True}))
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

        def _blob(self, sha: str, name: str):
            if not SHA_RE.match(sha) or not app.store.has(sha):
                return self._send(404, dumps({"error": "unknown version"}))
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            filename = os.path.basename(name).replace('"', "") or sha
            self._send(200, app.store.read(sha), ctype, {"Content-Disposition": f'inline; filename="{filename}"'})

        def _stream(self, params: dict):
            if not app.hub:
                return self._send(503, dumps({"error": "no database configured"}))
            q = app.hub.subscribe()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                self.wfile.write(b"retry: 3000\n\n")
                last = int(self.headers.get("Last-Event-ID") or params.get("after") or 0)
                if last:  # catch up on anything missed while disconnected
                    for row in reversed(app.events({"after": last, "limit": 500})):
                        last = self._emit(row)
                self.wfile.flush()
                while True:
                    try:
                        row = q.get(timeout=HEARTBEAT)
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        continue
                    if row["id"] > last:
                        last = self._emit(row)
                        self.wfile.flush()
            finally:
                app.hub.unsubscribe(q)

        def _emit(self, row: dict) -> int:
            self.wfile.write(b"id: %d\nevent: change\ndata: " % row["id"] + dumps(row) + b"\n\n")
            return row["id"]

    return Handler


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(prog="tracker.web")
    parser.add_argument("--host", default=os.environ.get("WEB_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("WEB_PORT", "8787")))
    parser.add_argument("--lookahead-log", type=Path,
                        help="record log the Lookahead timeline is folded from (default data/lookahead/log.jsonl, "
                             "or data/lookahead/rehearsal.jsonl with --rehearse)")
    parser.add_argument("--rehearse", type=Path, metavar="SCRIPT",
                        help="rehearse mode: replay this record script beat by beat on a scenario clock")
    args = parser.parse_args()
    load_env(PROJECT_ROOT / ".env")
    load_env(PROJECT_ROOT / "secrets" / "db.env")
    url = database_url()
    if not url and not args.rehearse:
        raise SystemExit("set DATABASE_URL or POSTGRES_PASSWORD (or run --rehearse without a database)")
    data_dir = Path(os.environ.get("TRACKER_DATA_DIR", PROJECT_ROOT / "data"))
    live_log = os.environ.get("LOOKAHEAD_LOG") or PROJECT_ROOT / "data" / "lookahead" / "log.jsonl"
    # A rehearsal never writes into the live log unless told to explicitly.
    log_path = args.lookahead_log or (PROJECT_ROOT / "data" / "lookahead" / "rehearsal.jsonl" if args.rehearse
                                      else Path(live_log))
    la = Lookahead(log_path, args.rehearse)
    app = App(url, data_dir, la)
    threading.Thread(target=la.run, daemon=True).start()
    if app.hub:
        threading.Thread(target=app.hub.run, daemon=True).start()
    else:
        log.warning("no database configured: only /lookahead works")
    server = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    server.daemon_threads = True
    log.info("serving on http://%s:%d", args.host, args.port)
    server.serve_forever()


if __name__ == "__main__":
    main()
