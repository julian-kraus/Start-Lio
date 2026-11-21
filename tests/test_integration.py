"""End-to-end: stub portal + real Postgres. Needs TEST_DATABASE_URL (a throwaway database; tables are dropped)."""

import hashlib
import itertools
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

import pytest

from conftest import make_pdf
from tracker import db, notify, poller
from tracker.client import PortalClient
from tracker.config import Settings, StaleRule
from tracker.consumer import subscribe
from tracker.store import BlobStore

DB_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="TEST_DATABASE_URL not set")
TOKEN = "test-token"


class Portal:
    def __init__(self, files: dict[str, bytes]):
        self.files = dict(files)
        self.downloads: list[str] = []
        self.served: dict[str, bytes] = {}   # serve different bytes than the listing announces
        self.missing: set[str] = set()       # listed but 404 on download
        self.extra_listing: list[dict] = []  # raw entries added to the listing as-is
        portal = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    return self._send(401, b'{"error":"unauthorized"}')
                if self.path == "/api/v1/files":
                    listing = [{"path": p, "bytes": len(b), "sha256": hashlib.sha256(b).hexdigest(), "url": ""}
                               for p, b in sorted(portal.files.items())] + portal.extra_listing
                    return self._send(200, json.dumps({"count": len(listing), "files": listing}).encode())
                path = unquote(self.path.removeprefix("/api/v1/files/"))
                if path in portal.files and path not in portal.missing:
                    portal.downloads.append(path)
                    return self._send(200, portal.served.get(path, portal.files[path]))
                self._send(404, b'{"error":"not found"}')

            def _send(self, status, body):
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"


class Webhook:
    def __init__(self):
        self.batches = []
        hook = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                hook.batches.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(204)
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/hook"


@pytest.fixture
def conn():
    c = db.connect(DB_URL)
    c.execute("SELECT pg_advisory_unlock_all()")
    c.execute("DROP TABLE IF EXISTS events, file_versions, polls, stale_flags, consumer_cursors CASCADE")
    c.commit()
    db.init_schema(c)
    yield c
    c.close()


def test_end_to_end(conn, tmp_path):
    shipments_v1 = b"shipment_id,eta,status\nSHP-1,2026-11-19,in_transit\nSHP-2,2026-11-20,booked\n"
    portal = Portal({
        "soony/erp/open_shipments.csv": shipments_v1,
        "soony/master/products.csv": b"product_id,name\nFG-1,Standard\n",
        "suppliers/SUP-1/contract.pdf": make_pdf(["Payment to IBAN DE00 1111"]),
    })
    settings = Settings(base_url=portal.url, token=TOKEN, database_url=DB_URL, data_dir=tmp_path,
                        poll_interval=1, stale_rules=(StaleRule("soony/erp/open_shipments.*", 0.5),),
                        webhook_urls=(), download_workers=4, diff_max_items=500)
    client = PortalClient(portal.url, TOKEN)
    store = BlobStore(tmp_path)

    # 1. Baseline: everything recorded, nothing emitted.
    first = poller.poll_once(settings, client, store, conn, baseline=True)
    assert first.events == [] and first.files == 3
    assert (tmp_path / "current/soony/master/products.csv").read_bytes().startswith(b"product_id")

    # 2. A consumer registered before the drop must catch up on it although it is not running yet.
    listener = db.connect(DB_URL, autocommit=True)
    listener.execute(f"LISTEN {db.NOTIFY_CHANNEL}")
    db.get_cursor(conn, "test-agent", start="latest")
    conn.commit()

    # 3. The drop: content change + format switch (csv -> json), a new notice, a removal.
    portal.files.pop("soony/erp/open_shipments.csv")
    portal.files["soony/erp/open_shipments.json"] = json.dumps([
        {"shipment_id": "SHP-1", "eta": "2026-11-23", "status": "in_transit"},
        {"shipment_id": "SHP-2", "eta": "2026-11-20", "status": "booked"},
    ]).encode()
    portal.files["suppliers/SUP-1/notices/bank_details_change.pdf"] = make_pdf(["New IBAN DE00 9999"])
    portal.files["suppliers/SUP-1/contract.pdf"] = make_pdf(["Payment to IBAN DE00 2222"])
    portal.files.pop("soony/master/products.csv")
    portal.downloads.clear()

    second = poller.poll_once(settings, client, store, conn)
    kinds = {e["path"]: e["kind"] for e in second.events}
    assert kinds == {
        "soony/erp/open_shipments.json": "renamed",
        "suppliers/SUP-1/notices/bank_details_change.pdf": "added",
        "suppliers/SUP-1/contract.pdf": "changed",
        "soony/master/products.csv": "removed",
    }
    assert sorted(portal.downloads) == sorted([
        "soony/erp/open_shipments.json", "suppliers/SUP-1/notices/bank_details_change.pdf",
        "suppliers/SUP-1/contract.pdf"])  # only changed files were downloaded

    renamed = conn.execute("SELECT * FROM events WHERE kind = 'renamed'").fetchone()
    assert renamed["old_path"] == "soony/erp/open_shipments.csv"
    assert renamed["diff"]["rows_changed"] == [{"key": "SHP-1", "changes": {"eta": ["2026-11-19", "2026-11-23"]}}]
    contract = conn.execute("SELECT diff FROM events WHERE kind = 'changed'").fetchone()["diff"]
    assert contract["lines_added"] == ["Payment to IBAN DE00 2222"]
    conn.commit()

    # NOTIFY arrived for every event.
    notes = list(listener.notifies(timeout=2, stop_after=4))
    assert {json.loads(n.payload)["kind"] for n in notes} == {"renamed", "added", "changed", "removed"}

    # History: the old contract version is still retrievable; the removed file left the current view.
    versions = conn.execute(
        "SELECT sha256 FROM file_versions WHERE path = 'suppliers/SUP-1/contract.pdf' ORDER BY version").fetchall()
    assert len(versions) == 2 and b"DE00 1111" in store.read(versions[0]["sha256"])
    assert not (tmp_path / "current/soony/master/products.csv").exists()
    conn.commit()

    # 4. The consumer catches up, filtered by path glob.
    got = list(itertools.islice(subscribe("test-agent", paths=["suppliers/*"], database_url=DB_URL), 2))
    assert sorted(e.kind for e in got) == ["added", "changed"]

    # 5. Silence: open_shipments goes stale, then recovers when it changes.
    time.sleep(0.7)
    third = poller.poll_once(settings, client, store, conn)
    assert [(e["kind"], e["path"]) for e in third.events] == [("stale", "soony/erp/open_shipments.json")]
    unchanged = poller.poll_once(settings, client, store, conn)
    assert unchanged.events == []  # flagged once per episode
    portal.files["soony/erp/open_shipments.json"] = b"[]"
    fourth = poller.poll_once(settings, client, store, conn)
    assert sorted(e["kind"] for e in fourth.events) == ["changed", "recovered"]

    # 6. Webhook receives everything after its cursor, in one batch per poll.
    hook = Webhook()
    conn.execute("INSERT INTO consumer_cursors (consumer, last_event_id) VALUES (%s, 0)", (notify.cursor_name(hook.url),))
    conn.commit()
    notify.dispatch_webhooks(conn, (hook.url,))
    total = conn.execute("SELECT count(*) AS n FROM events").fetchone()["n"]
    assert len(hook.batches) == 1 and len(hook.batches[0]["events"]) == total

    # 7. A wiped current view is restored from the blob store without downloads.
    for f in (tmp_path / "current").rglob("*"):
        if f.is_file():
            f.unlink()
    assert poller.reconcile_current(store, db.current_state(conn)) == 3
    listener.close()
