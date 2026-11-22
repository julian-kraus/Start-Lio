"""kb follows mail.events: a raw email stored by mail.store.ingest becomes labelled facts.
Runs when TEST_DATABASE_URL points at a throwaway Postgres. Each test has its own kb and mail schema. No network:
the LLM is a fake. Synthetic data only."""

import dataclasses
import hashlib
import json
import os
import shutil
import threading
import time
import uuid
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest
from psycopg import sql

from conftest import make_pdf
from kb import consumer, db, mail_source, rules
from kb.config import KbSettings
from kb.ingest import FileReader, process_file
from kb.llm.base import LLMClient, Routing
from kb.loader import load_contract
from kb.tabular import signature
from mail import config as mail_config, contacts
from mail import db as mail_db
from mail.store import BODY_FILENAME, ingest, put_blob
from tracker import db as tracker_db
from tracker.store import BlobStore

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")

CONTRACT = Path(__file__).resolve().parent.parent / "structured" / "contract"
OWN = "desk@example.com"
SUPPLIER, SUPPLIER_ADDR = "PARTNER-A", "sales@partner.example.net"
PERSONA, TEST_ADDR = "PERSONA-A", "player@team.example.org"
HEADER = ["code", "contact_email"]
MASTER_MAP = {"signature": signature(HEADER), "format": "csv", "fact_type": "supplier", "subject": ["supplier_id"],
              "columns": {"code": {"field": "supplier_id"}, "contact_email": {"field": "contact_email"}}}
SEED = "rules: []\nsettings:\n  confidence_map: {high: 0.9, medium: 0.75, low: 0.4}\n"


class FakeLLM(LLMClient):
    """One fact per document, like an adapter that read it. It also tries to forge what only mail may set."""
    name = "fake"
    needs_images = False

    def __init__(self, fact_type="supplier_statement"):
        self.fact_type = fact_type

    def classify(self, document, kinds):
        return Routing("kind", "supplier_email", "fake", "llm:fake")

    def extract(self, document, structure, schema, contract):
        return {"source": {}, "doc_kind": structure.name, "doc_kind_confidence": "high", "languages": ["en"],
                "spec_version": "1", "extracted_by": "fake", "doc_date": None, "unreadable": [],
                "proposed_fact_types": [],
                "facts": [{"fact_type": self.fact_type, "subject": {"supplier_id": "PARTNER-B"},
                           "attribute": "contact_email", "value": "intruder@elsewhere.example",
                           "locator": {"page": 1, "mail": {"trust": "supplier_contact", "scenario": False}}}]}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def raw_email(sender: str, *parts: tuple[str, bytes], body: str = "Dear desk, we confirm the delivery.") -> bytes:
    msg = MIMEMultipart()
    msg["From"], msg["To"], msg["Subject"] = sender, OWN, "Delivery"
    msg["Message-ID"] = f"<{uuid.uuid4().hex}@example.org>"
    msg.attach(MIMEText(body, "plain", "utf-8"))
    for name, data in parts:
        part = MIMEApplication(data, "octet-stream")
        part.add_header("Content-Disposition", "attachment", filename=name)
        msg.attach(part)
    return msg.as_bytes()


class Env:
    def __init__(self, tmp: Path):
        tag = uuid.uuid4().hex[:8]
        self.kb_schema, self.mail_schema = f"kb_ms_{tag}", f"mail_ms_{tag}"
        structured = tmp / "structured"
        shutil.copytree(CONTRACT, structured / "contract")
        (structured / "docs").mkdir()
        (structured / "schema_maps").mkdir()
        (structured / "schema_maps" / "master.json").write_text(json.dumps(MASTER_MAP))
        seed = tmp / "seed.yaml"
        seed.write_text(SEED)
        self.settings = KbSettings(database_url=URL, schema=self.kb_schema, data_dir=tmp / "data",
                                   structured_dir=structured, seed_path=seed, llm_adapter=None)
        self.conn = db.connect(URL, self.kb_schema)
        db.migrate(self.conn, self.kb_schema)
        tracker_db.init_schema(self.conn)
        rules.seed(self.conn, seed)
        load_contract(self.conn, structured)
        self.conn.commit()

        self.mail_dir = tmp / "mail_data"
        self.blobs = BlobStore(self.mail_dir)
        self.mail = mail_db.connect(URL, self.mail_schema)
        mail_db.migrate(self.mail, self.mail_schema)
        self.mail.execute("INSERT INTO contacts (address, supplier_id, source, is_test, persona_id) VALUES "
                          "(%s, %s, 'test', false, NULL), (%s, NULL, 'test', true, %s)",
                          (SUPPLIER_ADDR, SUPPLIER, TEST_ADDR, PERSONA))
        self.mail.commit()
        self.source = mail_source.MailSource(self.mail_schema, self.mail_dir)

    def receive(self, sender=SUPPLIER_ADDR, *parts, body="Dear desk, we confirm the delivery."):
        result = ingest(self.mail, self.blobs, raw_email(sender, *parts, body=body), OWN, "TST")
        self.mail.commit()
        return result

    def catch_up(self, llm=None):
        return consumer.catch_up(self.conn, self.settings, FileReader(self.settings.data_dir), llm, self.source)

    def rows(self, query: str, *args):
        found = self.conn.execute(query, args).fetchall()
        self.conn.commit()
        return found

    def documents(self):
        return {r["path"]: r for r in self.rows("SELECT * FROM documents WHERE active")}

    def cursor(self):
        with self.mail.transaction():
            rows = self.mail.execute("SELECT last_event_id FROM consumer_cursors").fetchall()
        return rows[0]["last_event_id"] if rows else None

    def emit(self, payload: dict) -> int:
        with self.mail.transaction():
            return mail_db.emit(self.mail, "document_added", payload)

    def close(self):
        for conn, schema in ((self.mail, self.mail_schema), (self.conn, self.kb_schema)):
            conn.rollback()
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
            conn.commit()
            conn.close()


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.close()


def wait(predicate, message, timeout=15.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, message
        time.sleep(0.05)


@pytest.fixture
def worker(env, monkeypatch):
    """`kb run` in a thread, configured through the environment like the compose service."""
    monkeypatch.setenv("MAIL_SCHEMA", env.mail_schema)
    monkeypatch.setenv("MAIL_DATA_DIR", str(env.mail_dir))
    monkeypatch.setattr(consumer, "load_adapter", lambda spec: FakeLLM())
    running = []

    def start(idle_timeout=0.2):
        stop = threading.Event()
        thread = threading.Thread(target=consumer.run, args=(env.settings, stop),
                                  kwargs={"idle_timeout": idle_timeout}, daemon=True)
        thread.start()

        def halt():
            stop.set()
            with psycopg.connect(URL, autocommit=True) as waker:
                waker.execute("SELECT pg_notify(%s, '')", (mail_source.CHANNEL,))
            thread.join(timeout=15)
            assert not thread.is_alive()

        running.append(halt)
        return thread, halt

    yield start
    for halt in running:
        halt()


def test_email_with_pdf_yields_facts_and_events_without_restart(env, worker):
    # The idle timeout is longer than the wait below, so only the mail notification can wake the worker.
    thread, _ = worker(idle_timeout=30)
    wait(lambda: env.cursor() == 0, "the worker never took up the mail cursor")

    result = env.receive(SUPPLIER_ADDR, ("confirmation.pdf", make_pdf(["Delivery confirmed"])))
    wait(lambda: len(env.rows("SELECT 1 FROM current_facts")) == 2, "the email did not become facts")

    facts = env.rows("SELECT * FROM current_facts")
    assert {f["path"].rsplit("/", 1)[1] for f in facts} == {BODY_FILENAME, "confirmation.pdf"}
    for fact in facts:
        assert fact["path"].startswith(f"mail/{SUPPLIER}/")
        label = fact["locator"]["mail"]
        assert label["trust"] == "supplier_contact" and label["scenario"] is False
        assert label["thread_id"] == result.thread_id and label["supplier_id"] == SUPPLIER
        assert fact["locator"]["page"] == 1
    changed = env.rows("SELECT path, change FROM fact_events")
    assert {e["path"] for e in changed} == {f["path"] for f in facts}
    assert {e["change"] for e in changed} == {"facts_added"}
    wait(lambda: env.cursor() == result.events[-1], "the cursor did not reach the last document")
    assert thread.is_alive()


def test_restart_sync_keeps_mail_documents_and_facts(env, worker):
    env.receive(SUPPLIER_ADDR, ("confirmation.pdf", make_pdf(["Confirmed"])))
    env.catch_up(FakeLLM())
    before = env.documents(), env.rows("SELECT fact_id FROM current_facts ORDER BY 1")
    runs = len(env.rows("SELECT 1 FROM runs"))

    worker()
    wait(lambda: len(env.rows("SELECT 1 FROM runs")) > runs, "the start-up sync did not finish")
    assert {p for p in env.documents() if p.startswith("mail/")} == before[0].keys()
    assert env.rows("SELECT fact_id FROM current_facts ORDER BY 1") == before[1]
    assert not env.rows("SELECT 1 FROM fact_events WHERE change = 'facts_removed'")


@pytest.mark.parametrize("kind", ["removed", "renamed", "added"])
def test_portal_events_cannot_touch_mail_paths(env, monkeypatch, kind):
    env.receive()
    env.catch_up(FakeLLM())
    (path, doc), = env.documents().items()
    event = {"kind": kind, "path": path, "old_path": None, "new_sha": sha(b"other")}
    event.update({"path": "portal/new.txt", "old_path": path} if kind == "renamed" else {})
    current = None if kind == "removed" else event["new_sha"]
    monkeypatch.setattr(consumer, "_current_sha", lambda conn, path: current)
    consumer.handle_event(env.conn, event, lambda *_: b"other", env.settings, {})
    env.conn.commit()
    assert env.documents()[path]["sha256"] == doc["sha256"]
    assert len(env.rows("SELECT 1 FROM current_facts WHERE path = %s", path)) == 1


def test_test_persona_facts_are_marked_as_scenario(env):
    result = env.receive(TEST_ADDR, ("note.pdf", make_pdf(["A note"])))
    env.catch_up(FakeLLM())
    facts = env.rows("SELECT locator FROM current_facts")
    assert len(facts) == 2
    for fact in facts:
        label = fact["locator"]["mail"]   # the fake claimed supplier_contact; the mail event wins
        assert label["trust"] == "test" and label["scenario"] is True
        assert label["persona_id"] == PERSONA and label["thread_id"] == result.thread_id
    # A portal document cannot claim a sender either.
    portal = SimpleNamespace(path="portal/a.txt", sha256=sha(b"a"))
    client = mail_source.LabellingClient(FakeLLM(), env.source, env.conn)
    assert client.extract(portal, SimpleNamespace(name="memo"), {}, {})["facts"][0]["locator"] == {"page": 1}


@pytest.mark.parametrize("route", ["extracted", "table"])
def test_mail_cannot_write_the_recipient_allowlist(env, route):
    if route == "table":
        env.receive(TEST_ADDR, ("master.csv", b"code,contact_email\nPARTNER-B,intruder@elsewhere.example\n"))
        env.catch_up()
    else:
        env.receive(TEST_ADDR)
        env.catch_up(FakeLLM("supplier"))
    facts = env.rows("SELECT fact_type, locator FROM current_facts")
    assert facts and {f["fact_type"] for f in facts} == {mail_source.CLAIMED_TYPE}
    assert all(f["locator"]["mail"]["trust"] == "test" for f in facts)
    settings = dataclasses.replace(mail_config.load_settings(load_files=False), kb_schema=env.kb_schema)
    assert contacts.from_kb(env.conn, settings) is None


def test_same_table_in_mail_and_portal_keeps_separate_facts(env):
    table = b"code,contact_email\nPARTNER-C,office@partner.example.net\n"
    env.receive(TEST_ADDR, ("master.csv", table))
    env.catch_up()
    process_file(env.conn, "portal/master.csv", sha(table), table, env.settings.structured_dir)
    env.conn.commit()
    env.receive(SUPPLIER_ADDR, ("master.csv", table))
    env.catch_up()
    facts = env.rows("SELECT path, fact_type, locator FROM current_facts")
    portal = [f for f in facts if not f["path"].startswith("mail/")]
    mailed = [f for f in facts if f["path"].startswith("mail/")]
    assert [f["fact_type"] for f in portal] == ["supplier"] and "mail" not in portal[0]["locator"]
    assert sorted(f["locator"]["mail"]["trust"] for f in mailed) == ["supplier_contact", "test"]
    assert {f["fact_type"] for f in mailed} == {mail_source.CLAIMED_TYPE}


def test_mailed_copy_does_not_revive_a_superseded_table(env):
    old = b"code,contact_email\nPARTNER-C,old@partner.example.net\n"
    new = b"code,contact_email\nPARTNER-C,new@partner.example.net\n"
    for data in (old, new):
        process_file(env.conn, "portal/master.csv", sha(data), data, env.settings.structured_dir)
        env.conn.commit()
    env.receive(TEST_ADDR, ("old.csv", old))
    env.catch_up()
    facts = env.rows("SELECT * FROM current_facts")
    portal = [f for f in facts if f["path"] == "portal/master.csv"]
    assert [f["value"] for f in portal] == ["new@partner.example.net"]
    (claim,) = [f for f in facts if f not in portal]   # the email's own claim, not master data
    assert claim["fact_type"] == mail_source.CLAIMED_TYPE and claim["locator"]["mail"]["scenario"] is True
    assert len(env.rows("SELECT 1 FROM facts WHERE fact_id NOT LIKE 'held:%%'")) == 3


def test_mailed_copy_does_not_load_a_portal_envelope(env):
    pdf = make_pdf(["Old confirmation"])
    envelope = {"source": {"path": "portal/old.pdf", "sha256": sha(pdf)}, "doc_kind": "offer", "spec_version": "1",
                "extracted_by": "w1", "facts": [{"fact_type": "offer", "subject": {"offer_no": "SYN"},
                                                 "attribute": "quantity", "value": "7"}]}
    (env.settings.structured_dir / "docs" / f"{sha(pdf)}.json").write_text(json.dumps(envelope))
    env.receive(TEST_ADDR, ("old.pdf", pdf))
    env.catch_up()
    assert "portal/old.pdf" not in env.documents() and env.rows("SELECT 1 FROM current_facts") == []


def test_portal_file_reusing_mail_facts_loses_the_mail_label(env, monkeypatch):
    pdf = make_pdf(["Profile"])
    env.receive(TEST_ADDR, ("profile.pdf", pdf))
    env.catch_up(FakeLLM("supplier"))
    path = f"portal/{uuid.uuid4().hex}.pdf"
    monkeypatch.setattr(consumer, "_current_sha", lambda conn, path: sha(pdf))
    consumer.handle_event(env.conn, {"kind": "added", "path": path, "new_sha": sha(pdf)}, lambda *_: pdf,
                          env.settings, {})
    env.conn.commit()
    (fact,) = env.rows("SELECT fact_type, locator FROM current_facts WHERE path = %s", path)
    assert fact["fact_type"] == "supplier" and "mail" not in fact["locator"]


def test_attachments_with_a_taken_name_do_not_replace_each_other(env):
    env.receive(SUPPLIER_ADDR, ("scan.pdf", make_pdf(["First"])), ("scan.pdf", make_pdf(["Second"])),
                (BODY_FILENAME, b"From: somebody else\nnot the body\n"))
    env.catch_up(FakeLLM())
    docs = env.documents()
    assert len(docs) == 4
    assert len(env.rows("SELECT 1 FROM current_facts")) == 4
    assert env.catch_up() is None   # none of them is processed again
    changed = len(env.rows("SELECT 1 FROM fact_events"))
    for event in env.mail.execute("SELECT payload FROM events WHERE kind = 'document_added'").fetchall():
        env.emit(event["payload"])
    assert env.catch_up(FakeLLM()) is None   # nor when its event is delivered twice
    assert len(env.rows("SELECT 1 FROM fact_events")) == changed == 4


def test_event_committed_below_the_cursor_is_still_read(env):
    data = b"From: partner\nlate commit\n"
    digest = sha(data)
    put_blob(env.blobs, data)
    with mail_db.connect(URL, env.mail_schema) as late:   # commits on leaving the block
        mail_db.emit(late, "document_added", {"path": "mail/LATE/a/note.txt", "sha256": digest, "trust": "test"})
        env.receive()               # takes a higher id and commits first
        env.catch_up()
    env.catch_up()
    assert "mail/LATE/a/note.txt" in env.documents()


def test_missing_blob_is_visible_and_retried_when_it_arrives(env):
    data = b"From: partner\narrives late\n"
    digest, path = sha(data), "mail/X/a/note.txt"
    env.emit({"path": path, "sha256": digest, "trust": "supplier_contact", "thread_id": "t"})
    env.catch_up()
    assert env.documents()[path]["status"] == "failed"
    assert env.rows("SELECT 1 FROM issues WHERE kind = 'ingest_failed' AND state = 'open'")
    assert env.catch_up() is None   # no busy retry while it is missing
    put_blob(env.blobs, data)
    env.catch_up()
    assert env.documents()[path]["status"] == "queued"

    corrupt = sha(b"expected")
    (env.blobs.objects / corrupt[:2]).mkdir(exist_ok=True)
    (env.blobs.objects / corrupt[:2] / corrupt).write_bytes(b"different")
    env.emit({"path": "mail/X/a/bad.txt", "sha256": corrupt, "trust": "supplier_contact"})
    env.receive()
    env.catch_up()   # a blob that does not match its hash fails without blocking the next document
    assert sorted(d["status"] for d in env.documents().values()) == ["failed", "queued", "queued"]


@pytest.mark.parametrize("change", [{"trust": "unknown_sender"}, {"path": "portal/a.txt"},
                                    {"path": "mail/../portal/a.txt"}, {"sha256": "../../x"}])
def test_untrusted_or_unsafe_event_is_ignored(env, change):
    event = env.emit({"path": "mail/X/a/a.txt", "sha256": sha(b"a"), "trust": "supplier_contact", **change})
    env.catch_up()
    assert env.documents() == {} and env.cursor() == event


def test_lost_connection_does_not_move_the_cursor(env, monkeypatch):
    env.receive()

    def lost(*args, **kwargs):
        raise psycopg.OperationalError("lost")
    monkeypatch.setattr(mail_source, "process_file", lost)
    with pytest.raises(psycopg.OperationalError):
        env.catch_up()
    env.conn.rollback()
    assert env.cursor() == 0


def test_no_mail_schema_and_no_mail_directory_is_fine(env, tmp_path):
    absent = tmp_path / "absent"
    source = mail_source.MailSource(f"nomail_{uuid.uuid4().hex[:6]}", absent)
    assert source.catch_up(env.conn, env.settings.structured_dir, {}) == []
    assert not absent.exists()
