"""Ingestion end to end: stub portal, tracker poll, kb worker and CLI, supplier email, replay adapter.

Synthetic data only. Every test gets its own database on the TEST_DATABASE_URL server, because the tracker tables live
in `public` and the worker lock is per database.
"""

import base64
import hashlib
import io
import json
import os
import shutil
import threading
import time
import uuid
import zipfile
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

import psycopg
import pytest
from psycopg import conninfo, sql

from conftest import make_pdf
from kb import __main__ as cli, consumer, db, rules
from kb.config import KbSettings
from kb.loader import load_contract
from kb.tabular import signature
from mail import db as mail_db
from mail.store import ingest
from tracker import db as tracker_db, poller
from tracker.client import PortalClient
from tracker.config import Settings
from tracker.store import BlobStore

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")

CONTRACT = Path(__file__).resolve().parent.parent / "structured" / "contract"
REPLAY = "kb.llm.replay:ReplayClient"
TOKEN = "test-token"
OWN, SUPPLIER_ADDRESS = "desk@example.com", "sales@partner.example.net"

PDF, SCAN, DOCX = "drop/confirmation.pdf", "drop/note.png", "drop/change.docx"
CSV, NEWS, NEW_NEWS = "drop/receipts.csv", "drop/news.json", "drop/news_new_layout.json"
CSV_HEADER, NEWS_HEADER = ["receipt", "note", "qty"], ["news_id", "headline"]
PIXEL = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
# Doc kinds of this test only: the contract's own kinds may change how they recognise files.
KINDS = {
    "test_confirmation": {"container": "pdf", "path_globs": ["drop/*"]},
    "test_note": {"container": "image", "path_globs": ["drop/*"]},
    "test_change": {"container": "docx", "path_globs": ["drop/*"]},
    "test_email": {"container": "text", "path_globs": ["mail/*"]},
    "test_attachment": {"container": "pdf", "path_globs": ["mail/*"]},
}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_docx(text: str) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("word/document.xml", f"<w:document><w:p><w:t>{text}</w:t></w:p></w:document>")
    return out.getvalue()


def documents() -> dict[str, bytes]:
    # more than 200 characters of text, so the gate looks every number up in it
    lines = ["Order confirmation OC-TEST for order PO-TEST", "We confirm 120 pieces of part PART-TEST"]
    lines += [f"Standard terms, paragraph {word}, apply to this order" for word in ("one", "two", "three", "four")]
    return {PDF: make_pdf(lines), SCAN: PIXEL, DOCX: make_docx("Change CR-TEST moves the hole")}


def tables() -> dict[str, bytes]:
    return {CSV: b"receipt_no,delivery_note,quantity\nR-TEST,N-TEST,140\n",
            NEWS: json.dumps([{"news_id": "NEWS-TEST", "headline": "Port closed"}]).encode(),
            NEW_NEWS: json.dumps({"items": [{"ref": "NEWS-NEXT", "title": "Port open"}]}).encode()}


def fact(fact_type: str, subject: dict, attribute: str, value) -> dict:
    return {"fact_type": fact_type, "subject": subject, "attribute": attribute, "value": value,
            "evidence": str(value), "confidence": "high", "locator": {"page": 1}}


GOOD = {
    PDF: fact("oc_line", {"oc": "OC-TEST", "part": "PART-TEST"}, "qty", 120),
    SCAN: fact("delivery_note_line", {"note": "N-TEST", "part": "PART-TEST"}, "qty", 130),
    DOCX: fact("change_record", {"change": "CR-TEST"}, "description", "moves the hole"),
}


class Portal:
    def __init__(self, files: dict[str, bytes]):
        self.files = dict(files)
        portal = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    return self.reply(401, b"{}")
                if self.path == "/api/v1/files":
                    files = [{"path": path, "bytes": len(data), "sha256": sha(data), "url": ""}
                             for path, data in sorted(portal.files.items())]
                    return self.reply(200, json.dumps({"count": len(files), "files": files}).encode())
                path = unquote(self.path.removeprefix("/api/v1/files/"))
                if path in portal.files:
                    return self.reply(200, portal.files[path])
                self.reply(404, b"{}")

            def reply(self, status: int, body: bytes):
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Pipeline:
    """One database, one portal and one mailbox, driven the way the deployed processes drive them."""

    def __init__(self, url: str, tmp: Path, portal: Portal, monkeypatch, capsys):
        self.portal, self.capsys = portal, capsys
        structured, seed = tmp / "structured", tmp / "seed.yaml"
        self.recorded = tmp / "recorded"
        for folder in (structured / "docs", structured / "schema_maps", structured / "contract" / "doc_kinds",
                       self.recorded / "docs"):
            folder.mkdir(parents=True)
        for name in ("fact_types.json", "envelope.schema.json"):
            shutil.copy(CONTRACT / name, structured / "contract" / name)
        for name, recognise in KINDS.items():
            (structured / "contract" / "doc_kinds" / f"{name}.md").write_text(
                f"---\nrecognise: {json.dumps(recognise)}\n---\nRead every stated fact.\n")
        for format_, header, fact_type in (("csv", CSV_HEADER, "goods_receipt"),
                                           ("json", NEWS_HEADER, "x_news_record")):
            (structured / "schema_maps" / f"{format_}.json").write_text(json.dumps({
                "signature": signature(header), "format": format_, "fact_type": fact_type, "subject": [header[0]],
                "columns": {column: {"field": column, "type": "text"} for column in header}}))
        seed.write_text("rules: []\nsettings:\n  confidence_map: {high: 0.9, medium: 0.75, low: 0.4}\n")

        self.settings = KbSettings(database_url=url, schema="kb", data_dir=tmp / "data", structured_dir=structured,
                                   seed_path=seed, llm_adapter=REPLAY)
        monkeypatch.setattr(cli, "load_settings", lambda: self.settings)
        monkeypatch.setenv("KB_STRUCTURED_DIR", str(self.recorded))  # where the replay adapter finds its answers
        monkeypatch.setenv("MAIL_SCHEMA", "mail")
        monkeypatch.setenv("MAIL_DATA_DIR", str(tmp / "mail_data"))

        self.conn = db.connect(url, "kb", autocommit=True)
        with db.connect(url, "kb") as setup:
            db.migrate(setup, "kb")
            rules.seed(setup, seed)
            load_contract(setup, structured)
            setup.commit()
        self.tracker = tracker_db.connect(url)
        tracker_db.init_schema(self.tracker)
        self.tracker.commit()
        self.poll_settings = Settings(base_url=portal.url, token=TOKEN, database_url=url,
                                      data_dir=self.settings.data_dir, poll_interval=1, stale_rules=(),
                                      webhook_urls=(), download_workers=4, diff_max_items=500)
        self.store, self.client = BlobStore(self.settings.data_dir), PortalClient(portal.url, TOKEN)
        self.mail = mail_db.connect(url, "mail")
        mail_db.migrate(self.mail, "mail")
        self.mail.execute("INSERT INTO contacts (address, supplier_id, source, is_test, persona_id) "
                          "VALUES (%s, 'PARTNER-TEST', 'test', false, NULL)", (SUPPLIER_ADDRESS,))
        self.mail.commit()
        self.mailbox = BlobStore(tmp / "mail_data")
        self.thread, self.stopping = None, threading.Event()
        self.poll(baseline=True)

    def rows(self, query: str, *args) -> list[dict]:
        return self.conn.execute(query, args).fetchall()

    def poll(self, baseline: bool = False):
        result = poller.poll_once(self.poll_settings, self.client, self.store, self.tracker, baseline=baseline)
        assert not result.failed
        return result

    def drop(self, files: dict[str, bytes]):
        self.portal.files.update(files)
        return self.poll()

    def record(self, data: bytes, *facts: dict):
        (self.recorded / "docs" / f"{sha(data)}.json").write_text(json.dumps({
            "source": {}, "doc_kind": "", "doc_kind_confidence": "high", "languages": ["en"], "spec_version": "1",
            "extracted_by": "replay", "doc_date": None, "facts": list(facts), "unreadable": [],
            "proposed_fact_types": []}))

    def start(self, adapter: str | None = REPLAY):
        settings = KbSettings(**{**vars(self.settings), "llm_adapter": adapter})
        self.thread = threading.Thread(target=consumer.run, args=(settings, self.stopping, 0.2), daemon=True)
        self.thread.start()
        # the feed is followed from here on: what the portal drops next arrives as events, not by start-up sync
        self.wait(lambda: self.rows("SELECT 1 FROM public.consumer_cursors WHERE consumer = %s", consumer.CONSUMER))

    def wait(self, condition, timeout: float = 20):
        deadline = time.monotonic() + timeout
        while not condition():
            assert self.thread.is_alive(), "the kb worker died"
            assert time.monotonic() < deadline, f"timed out; documents: {self.statuses()}"
            time.sleep(0.05)

    def stop(self):
        """Returns after the worker's current round, so its reconcile has been committed."""
        if self.thread:
            self.stopping.set()
            self.thread.join(20)
            assert not self.thread.is_alive(), "the kb worker did not stop"

    def statuses(self) -> dict[str, str]:
        return {r["path"]: r["status"] for r in self.rows("SELECT path, status FROM documents WHERE active")}

    def settle(self, expected: int):
        """Wait until the worker has seen `expected` documents and left none pending, then stop it."""
        self.wait(lambda: len(self.statuses()) == expected
                  and not self.rows("SELECT 1 FROM extraction_queue WHERE state = 'pending'"))
        self.stop()

    def kb(self, *args: str) -> str:
        self.capsys.readouterr()
        assert cli.main(list(args)) == 0
        return self.capsys.readouterr().out

    def outcomes(self) -> dict[str, set[str]]:
        """What each active document ended as: `facts`, named issues, or an empty set if it was silently dropped."""
        found = {path: set() for path in self.statuses()}
        for row in self.rows("SELECT DISTINCT source_path AS path FROM facts WHERE valid"):
            found.setdefault(row["path"], set()).add("facts")
        for row in self.rows("SELECT kind, payload->>'path' AS path FROM issues WHERE state = 'open'"):
            if row["path"]:
                found.setdefault(row["path"], set()).add(row["kind"])
        return found

    def receive_email(self, attachment: bytes) -> list[str]:
        """A supplier email through mail's own ingest. Returns the kb paths it announced."""
        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = SUPPLIER_ADDRESS, OWN, "Delivery"
        message["Message-ID"] = f"<{uuid.uuid4().hex}@partner.example.net>"
        message.set_content("Dear desk, we confirm the delivery.")
        message.add_attachment(attachment, maintype="application", subtype="pdf", filename="confirmation.pdf")
        with self.mail.transaction():
            ingest(self.mail, self.mailbox, message.as_bytes(), OWN, "TST")
            added = [r["payload"] for r in self.mail.execute(
                "SELECT payload FROM events WHERE kind = 'document_added'")]
            for part in added:  # recorded before the commit, which wakes the worker
                self.record(self.mailbox.read(part["sha256"]),
                            fact("supplier_statement", {"document": part["sha256"][:12]}, "confirmed", "delivery"))
        return [part["path"] for part in added]

    def close(self):
        try:
            self.stop()
        finally:
            for conn in (self.conn, self.tracker, self.mail):
                conn.close()


@pytest.fixture
def pipeline(tmp_path, monkeypatch, capsys):
    name = f"ingest_e2e_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(URL, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    portal = Portal({CSV: b"receipt,note,qty\nR-TEST,N-TEST,140\n"})
    created = None
    try:
        created = Pipeline(conninfo.make_conninfo(URL, dbname=name), tmp_path, portal, monkeypatch, capsys)
        yield created
    finally:
        if created:
            created.close()
        portal.close()
        with psycopg.connect(URL, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def test_each_dropped_format_ends_as_facts_or_a_named_issue(pipeline):
    docs = documents()
    for path, data in docs.items():
        pipeline.record(data, GOOD[path])
    pipeline.start()
    assert pipeline.statuses() == {CSV: "parsed"}
    assert pipeline.outcomes() == {CSV: {"facts"}}

    result = pipeline.drop({**docs, **tables()})
    assert {e["path"]: e["kind"] for e in result.events} == {
        PDF: "added", SCAN: "added", DOCX: "added", CSV: "changed", NEWS: "added", NEW_NEWS: "added"}
    pipeline.settle(expected=6)

    assert pipeline.statuses() == {PDF: "loaded", SCAN: "loaded", DOCX: "loaded", NEWS: "parsed",
                                   CSV: "needs_schema_map", NEW_NEWS: "needs_schema_map"}
    # the renamed CSV no longer counts with the facts of its old layout
    assert pipeline.outcomes() == {PDF: {"facts"}, SCAN: {"facts"}, DOCX: {"facts"}, NEWS: {"facts"},
                                   CSV: {"schema_map_needed"}, NEW_NEWS: {"schema_map_needed"}}
    read = {r["source_path"]: (r["value"], r["extractor"]) for r in pipeline.rows(
        "SELECT source_path, value, extractor FROM facts WHERE valid AND source_path = ANY(%s)", list(docs))}
    assert read == {path: (GOOD[path]["value"], "llm:replay") for path in docs}
    assert json.loads(pipeline.kb("status"))["queue"] == {"done": 3}


@pytest.mark.parametrize("path, bad, reason", [
    (PDF, fact("oc_line", {"oc": "OC-TEST"}, "qty", 987), "do not occur in the text layer"),
    (SCAN, fact("delivery_note_line", {"line": "row 1"}, "qty", 130), "row number"),
    (DOCX, fact("not_in_contract", {"change": "CR-TEST"}, "description", "moves"), "outside the contract"),
], ids=["text pdf", "scan", "docx"])
def test_answer_that_fails_the_gate_ends_in_review(pipeline, path, bad, reason):
    data = documents()[path]
    pipeline.record(data, bad)
    pipeline.start()
    pipeline.drop({path: data})
    pipeline.settle(expected=2)

    assert pipeline.statuses()[path] == "needs_review"
    assert pipeline.outcomes()[path] == {"review_needed"}
    (issue,) = pipeline.rows("SELECT title FROM issues WHERE kind = 'review_needed' AND state = 'open'")
    assert reason in issue["title"]
    assert json.loads(pipeline.kb("status"))["queue"] == {"review": 1}


def test_backfill_drains_what_queued_without_an_adapter(pipeline):
    docs = documents()
    pipeline.record(docs[PDF], GOOD[PDF])
    pipeline.record(docs[SCAN], fact("delivery_note_line", {"line": "row 1"}, "qty", 130))
    # nothing is recorded for the DOCX: the adapter fails on it every time
    pipeline.start(adapter=None)
    pipeline.drop(docs)
    pipeline.wait(lambda: len(pipeline.statuses()) == 4)
    pipeline.stop()
    waiting = pipeline.outcomes()
    assert all(waiting[path] == {"pending_extraction"} for path in docs)
    assert json.loads(pipeline.kb("status"))["queue"] == {"pending": 3}

    for _ in range(3):  # ops/backfill.md: three runs, a failing document gets three attempts
        pipeline.kb("extract", "--adapter", REPLAY, "--limit", "100")

    assert pipeline.outcomes() == {CSV: {"facts"}, PDF: {"facts"}, SCAN: {"review_needed"},
                                   DOCX: {"extraction_failed"}}
    assert json.loads(pipeline.kb("status"))["queue"] == {"done": 1, "review": 1, "failed": 1}


def test_document_of_no_known_kind_stays_visible(pipeline):
    path, data = "elsewhere/memo.txt", b"A memo that fits no known kind."
    pipeline.start()
    pipeline.drop({path: data})
    pipeline.wait(lambda: path in pipeline.statuses())
    pipeline.stop()
    assert pipeline.outcomes()[path] == {"pending_extraction"}


def test_backfill_first_document_gets_past_one_of_no_kind(pipeline):
    data = documents()[PDF]
    pipeline.record(data, GOOD[PDF])
    pipeline.start(adapter=None)
    for path, blob in (("elsewhere/memo.txt", b"A memo that fits no known kind."), (PDF, data)):
        pipeline.drop({path: blob})
        pipeline.wait(lambda: path in pipeline.statuses())
    pipeline.stop()
    for limit, done in ((1, 0), (1, 0), (2, 1)):  # ops/backfill.md step 4: the same limit never gets past it
        printed, _ = json.JSONDecoder().raw_decode(pipeline.kb("extract", "--adapter", REPLAY, "--limit", str(limit)))
        assert (printed["unrouted"], printed["done"]) == (1, done)


@pytest.mark.parametrize("adapter", [REPLAY, None], ids=["worker", "backfill"])
def test_supplier_email_after_the_drop_ends_as_facts(pipeline, adapter):
    docs = documents()
    for path, data in docs.items():
        pipeline.record(data, GOOD[path])
    pipeline.start(adapter)
    pipeline.drop({**docs, **tables()})
    pipeline.wait(lambda: len(pipeline.statuses()) == 6)

    mailed = pipeline.receive_email(make_pdf(["Partner confirms the delivery"]))
    assert len(mailed) == 2  # body and attachment
    if adapter:
        pipeline.settle(expected=8)
    else:
        pipeline.wait(lambda: len(pipeline.statuses()) == 8)
        pipeline.stop()
        pipeline.kb("extract", "--adapter", REPLAY, "--limit", "100")

    outcomes = pipeline.outcomes()
    assert len(outcomes) == 8
    assert all(outcomes.values()), f"silently dropped: {outcomes}"
    assert all(outcomes[path] == {"facts"} for path in [*mailed, *docs, NEWS])
    labels = pipeline.rows("SELECT locator->'mail' AS mail FROM facts WHERE valid AND source_path = ANY(%s)", mailed)
    assert len(labels) == 2 and {r["mail"]["trust"] for r in labels} == {"supplier_contact"}
    assert json.loads(pipeline.kb("status"))["queue"] == {"done": 5}
