"""Local web UI for change events: python -m tracker.web [--port 8787].

GET /                    the feed page
GET /api/status          last poll and totals
GET /api/events          newest first; ?before=<id>&limit=&kind=&q=
GET /api/events/<id>     one event with its full diff
GET /api/stream          Server-Sent Events, one message per new event (resumes via Last-Event-ID)
GET /api/blob/<sha256>   raw content of a stored file version
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
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import psycopg

from . import db
from .config import PROJECT_ROOT, database_url, load_env
from .store import BlobStore

log = logging.getLogger("tracker.web")
PAGE = Path(__file__).with_name("web_page.html")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
LIST_COLUMNS = "id, at, kind, path, old_path, old_sha, new_sha, summary"
HEARTBEAT = 15


def _json_default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
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
    def __init__(self, url: str, data_dir: Path):
        self.url = url
        self.store = BlobStore(data_dir)
        self.hub = Hub(url)
        self.local = threading.local()

    def conn(self) -> psycopg.Connection:
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
                if url.path == "/api/stream":
                    return self._stream(params)
                self._send(404, dumps({"error": "not found"}))
            except (BrokenPipeError, ConnectionResetError):
                pass
            except (ValueError, psycopg.Error) as exc:
                self._send(400 if isinstance(exc, ValueError) else 503, dumps({"error": str(exc)}))

        def _blob(self, sha: str, name: str):
            if not SHA_RE.match(sha) or not app.store.has(sha):
                return self._send(404, dumps({"error": "unknown version"}))
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            filename = os.path.basename(name).replace('"', "") or sha
            self._send(200, app.store.read(sha), ctype, {"Content-Disposition": f'inline; filename="{filename}"'})

        def _stream(self, params: dict):
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
    args = parser.parse_args()
    load_env(PROJECT_ROOT / ".env")
    load_env(PROJECT_ROOT / "secrets" / "db.env")
    url = database_url()
    if not url:
        raise SystemExit("set DATABASE_URL or POSTGRES_PASSWORD")
    app = App(url, Path(os.environ.get("TRACKER_DATA_DIR", PROJECT_ROOT / "data")))
    threading.Thread(target=app.hub.run, daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    server.daemon_threads = True
    log.info("serving on http://%s:%d", args.host, args.port)
    server.serve_forever()


if __name__ == "__main__":
    main()
