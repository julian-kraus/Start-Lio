"""kb follows mail.events (synthetic data, fake LLM, own schemas per test on the TEST_DATABASE_URL Postgres)."""

import dataclasses
import hashlib
import json
import os
import shutil
import threading
import time
import uuid
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import psycopg
import pytest
from psycopg import sql

from conftest import make_pdf
from kb import consumer, db, facts as facts_mod, mail_source, rules
from kb.config import KbSettings
from kb.ingest import FileReader, process_file
from kb.llm.base import LLMClient, Routing, StructureProposal
from kb.loader import load_contract
from kb.tabular import signature
from mail import config as mail_config, contacts, db as mail_db
from mail.store import BODY_FILENAME, ingest, put_blob
from tracker import db as tracker_db
from tracker.store import BlobStore

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")
CONTRACT = Path(__file__).resolve().parent.parent / "structured" / "contract"
OWN, SUPPLIER, SUPPLIER_ADDR = "desk@example.com", "PARTNER-A", "sales@partner.example.net"
PERSONA, TEST_ADDR, FORGED = "PERSONA-A", "player@team.example.org", "2001-01-01T00:00:00+00:00"


def master_map(code: str, email: str) -> dict:
    return {"signature": signature([code, email]), "format": "csv", "fact_type": "supplier", "subject": ["supplier_id"],
            "columns": {code: {"field": "supplier_id"}, email: {"field": "contact_email"}}}


class FakeLLM(LLMClient):
    """One fact per document. It tries to forge what only mail may set and to overwrite what is shared."""
    name, needs_images = "fake", False

    def __init__(self, fact_type="supplier_statement", kind="supplier_email"):
        self.fact_type, self.kind, self.classified = fact_type, kind, 0

    def classify(self, document, kinds):
        self.classified += 1
        return Routing("kind" if self.kind else "none", self.kind, "fake", "llm:fake")

    def design(self, document, similar, contract, sample_rows=None):
        if sample_rows:
            return StructureProposal("schema_map", "new map", "fake", schema_map=master_map("id", "email"))
        return StructureProposal("doc_kind", "order_confirmation", "fake", description="rules from the sender")

    def extract(self, document, structure, schema, contract):
        mail = {"trust": "supplier_contact", "scenario": False, "sent_at": FORGED, "date_header": FORGED}
        fact = {"fact_type": self.fact_type, "subject": {"supplier_id": "PARTNER-B"}, "attribute": "contact_email",
                "value": "intruder@elsewhere.example", "locator": {"page": 1, "mail": mail}}
        return {"source": {}, "doc_kind": structure.name, "doc_kind_confidence": "high", "languages": ["en"],
                "spec_version": "1", "extracted_by": "fake", "doc_date": None, "unreadable": [], "facts": [fact],
                "proposed_fact_types": [{"name": "x_from_sender", "description": "text for the next prompt"}]}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def raw_email(sender: str, *parts: tuple[str, bytes]) -> bytes:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, OWN, "Delivery"
    msg["Message-ID"] = f"<{uuid.uuid4().hex}@example.org>"
    msg.set_content("Dear desk, we confirm the delivery.")
    for name, data in parts:
        msg.add_attachment(data, maintype="application", subtype="octet-stream", filename=name)
    return msg.as_bytes()


class Env:
    def __init__(self, tmp: Path):
        tag = uuid.uuid4().hex[:8]
        self.kb_schema, self.mail_schema = f"kb_ms_{tag}", f"mail_ms_{tag}"
        structured, seed = tmp / "structured", tmp / "seed.yaml"
        shutil.copytree(CONTRACT, structured / "contract")
        for folder in ("docs", "schema_maps"):
            (structured / folder).mkdir()
        (structured / "schema_maps" / "master.json").write_text(json.dumps(master_map("code", "contact_email")))
        seed.write_text("rules: []\nsettings:\n  confidence_map: {high: 0.9, medium: 0.75, low: 0.4}\n")
        self.settings = KbSettings(database_url=URL, schema=self.kb_schema, data_dir=tmp / "data",
                                   structured_dir=structured, seed_path=seed, llm_adapter=None)
        self.conn, self.mail = db.connect(URL, self.kb_schema), mail_db.connect(URL, self.mail_schema)
        db.migrate(self.conn, self.kb_schema)
        tracker_db.init_schema(self.conn)
        rules.seed(self.conn, seed)
        load_contract(self.conn, structured)
        self.conn.commit()
        self.mail_dir, self.blobs = tmp / "mail_data", BlobStore(tmp / "mail_data")
        mail_db.migrate(self.mail, self.mail_schema)
        self.mail.execute("INSERT INTO contacts (address, supplier_id, source, is_test, persona_id) VALUES "
                          "(%s, %s, 'test', false, NULL), (%s, NULL, 'test', true, %s)",
                          (SUPPLIER_ADDR, SUPPLIER, TEST_ADDR, PERSONA))
        self.mail.commit()
        self.source = mail_source.MailSource(self.mail_schema, self.mail_dir)

    def receive(self, sender=SUPPLIER_ADDR, *parts):
        with self.mail.transaction():
            return ingest(self.mail, self.blobs, raw_email(sender, *parts), OWN, "TST")

    def deliver(self, sender=SUPPLIER_ADDR, *parts, llm=None):
        result = self.receive(sender, *parts)
        self.catch_up(llm)
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
            row = self.mail.execute("SELECT last_event_id FROM consumer_cursors").fetchone()
        return row and row["last_event_id"]

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
    yield (created := Env(tmp_path))
    created.close()


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
        thread = threading.Thread(target=consumer.run, args=(env.settings, stop, idle_timeout), daemon=True)
        thread.start()
        running.append((stop, thread))
        return thread
    yield start
    for stop, thread in running:
        stop.set()
        with psycopg.connect(URL, autocommit=True) as waker:
            waker.execute("SELECT pg_notify(%s, '')", (mail_source.CHANNEL,))
        thread.join(timeout=15)
        assert not thread.is_alive()


def test_email_with_pdf_yields_facts_and_events_without_restart(env, worker):
    thread = worker(idle_timeout=30)   # longer than the wait below, so only the mail notification can wake it
    wait(lambda: env.cursor() == 0, "the worker never took up the mail cursor")
    result = env.receive(SUPPLIER_ADDR, ("confirmation.pdf", make_pdf(["Delivery confirmed"])))
    wait(lambda: len(env.rows("SELECT 1 FROM current_facts")) == 2, "the email did not become facts")
    facts = env.rows("SELECT * FROM current_facts")
    assert {f["path"].rsplit("/", 1)[1] for f in facts} == {BODY_FILENAME, "confirmation.pdf"}
    for fact in facts:
        assert fact["path"].startswith(f"mail/{SUPPLIER}/")
        label = fact["locator"]["mail"]
        assert label["trust"] == "supplier_contact" and label["scenario"] is False and fact["locator"]["page"] == 1
        assert label["thread_id"] == result.thread_id and label["supplier_id"] == SUPPLIER
    changed = {(e["path"], e["change"]) for e in env.rows("SELECT path, change FROM fact_events")}
    assert changed == {(f["path"], "facts_added") for f in facts}
    wait(lambda: env.cursor() == result.events[-1], "the cursor did not reach the last document")
    assert thread.is_alive()


def test_restart_sync_keeps_mail_documents_and_facts(env, worker):
    env.deliver(SUPPLIER_ADDR, ("confirmation.pdf", make_pdf(["Confirmed"])), llm=FakeLLM())
    before = env.documents(), env.rows("SELECT fact_id FROM current_facts ORDER BY 1")
    runs = len(env.rows("SELECT 1 FROM runs"))
    worker()
    wait(lambda: len(env.rows("SELECT 1 FROM runs")) > runs, "the start-up sync did not finish")
    assert {p for p in env.documents() if p.startswith("mail/")} == before[0].keys()
    assert env.rows("SELECT fact_id FROM current_facts ORDER BY 1") == before[1]
    assert not env.rows("SELECT 1 FROM fact_events WHERE change = 'facts_removed'")


@pytest.mark.parametrize("kind", ["removed", "renamed", "added"])
def test_portal_events_cannot_touch_mail_paths(env, monkeypatch, kind):
    env.deliver(llm=FakeLLM())
    (path, doc), = env.documents().items()
    event = {"kind": kind, "path": path, "old_path": None, "new_sha": sha(b"other")}
    event.update({"path": "portal/new.txt", "old_path": path} if kind == "renamed" else {})
    monkeypatch.setattr(consumer, "_current_sha", lambda *_: None if kind == "removed" else event["new_sha"])
    consumer.handle_event(env.conn, event, lambda *_: b"other", env.settings, {})
    env.conn.commit()
    assert env.documents()[path]["sha256"] == doc["sha256"]
    assert len(env.rows("SELECT 1 FROM current_facts WHERE path = %s", path)) == 1


def test_test_persona_facts_are_marked_as_scenario(env):
    result = env.deliver(TEST_ADDR, ("a.pdf", make_pdf(["A"])), ("b.csv", b"code,contact_email\nC,c\n"), llm=FakeLLM())
    events = env.rows(f"SELECT payload FROM {env.mail_schema}.events WHERE kind = 'document_added'")
    assert len(facts := env.rows(stored := "SELECT fact_id, locator, valid FROM facts ORDER BY 1")) == 3
    for fact in facts:
        label = fact["locator"]["mail"]   # the fake claimed supplier_contact and a time; the mail event wins
        assert label["trust"] == "test" and label["scenario"] is True and label["persona_id"] == PERSONA
        assert label["thread_id"] == result.thread_id and "date_header" not in label
        assert label["sent_at"] == events[0]["payload"]["received_at"]
    env.conn.execute("UPDATE extraction_queue SET state = 'pending'")
    assert all(env.emit(event["payload"]) for event in events)   # the same events again, and a second extraction
    env.catch_up(FakeLLM())
    assert not env.rows("SELECT 1 FROM extraction_queue WHERE state = 'pending'") and env.rows(stored) == facts
    assert not env.rows("SELECT 1 FROM fact_types WHERE name = 'x_from_sender'")
    portal = SimpleNamespace(path="portal/a.txt", sha256=sha(b"a"))   # cannot claim a sender either
    client = mail_source.LabellingClient(FakeLLM(), env.source, env.conn)
    assert client.extract(portal, SimpleNamespace(name="memo"), {}, {})["facts"][0]["locator"] == {"page": 1}


@pytest.mark.parametrize("route", ["extracted", "table", "reused"])
def test_mail_cannot_write_the_recipient_allowlist(env, route):
    if route == "table":
        env.deliver(TEST_ADDR, ("master.csv", b"code,contact_email\nPARTNER-B,intruder@elsewhere.example\n"))
    elif route == "extracted":
        env.deliver(TEST_ADDR, llm=FakeLLM("supplier"))
    else:   # the bytes of a portal document that already has master data facts
        pdf = make_pdf(["Profile"])
        fact = {"fact_type": "supplier", "subject": {"supplier_id": "PARTNER-B"}, "attribute": "language",
                "value": "en", "extractor": "fake", "source_path": "portal/profile.pdf", "source_sha256": sha(pdf)}
        facts_mod.insert_facts(env.conn, [fact])
        env.deliver(TEST_ADDR, ("profile.pdf", pdf))
    facts = env.rows("SELECT fact_type, locator FROM current_facts WHERE starts_with(path, 'mail/')")
    announced = env.rows("SELECT fact_types FROM fact_events WHERE starts_with(path, 'mail/') AND n_facts > 0")
    assert announced and all(e["fact_types"] == [mail_source.CLAIMED_TYPE] for e in announced)
    assert facts and {f["fact_type"] for f in facts} == {mail_source.CLAIMED_TYPE}
    assert all(f["locator"]["mail"]["trust"] == "test" and f["locator"]["mail"]["sent_at"] for f in facts)
    settings = dataclasses.replace(mail_config.load_settings(load_files=False), kb_schema=env.kb_schema)
    assert contacts.from_kb(env.conn, settings) is None or route == "reused"   # that one is the portal's


def test_same_table_in_mail_and_portal_keeps_separate_facts(env):
    table = b"code,contact_email\nPARTNER-C,office@partner.example.net\n"
    env.deliver(TEST_ADDR, ("master.csv", table))
    process_file(env.conn, "portal/master.csv", sha(table), table, env.settings.structured_dir)
    env.conn.commit()
    env.deliver(SUPPLIER_ADDR, ("master.csv", table))
    facts = env.rows("SELECT path, fact_type, locator FROM current_facts")
    portal, mailed = ([f for f in facts if f["path"].startswith(top)] for top in ("portal/", "mail/"))
    assert [f["fact_type"] for f in portal] == ["supplier"] and "mail" not in portal[0]["locator"]
    assert sorted(f["locator"]["mail"]["trust"] for f in mailed) == ["supplier_contact", "test"]
    assert {f["fact_type"] for f in mailed} == {mail_source.CLAIMED_TYPE}


def test_mailed_copy_does_not_revive_a_superseded_table(env):
    old, new = (f"code,contact_email\nPARTNER-C,{who}@partner.example.net\n".encode() for who in ("old", "new"))
    for data in (old, new):
        process_file(env.conn, "portal/master.csv", sha(data), data, env.settings.structured_dir)
        env.conn.commit()
    env.deliver(TEST_ADDR, ("old.csv", old))
    facts = env.rows("SELECT * FROM current_facts")
    portal = [f for f in facts if f["path"] == "portal/master.csv"]
    assert [f["value"] for f in portal] == ["new@partner.example.net"]
    (claim,) = [f for f in facts if f not in portal]   # the email's own claim, not master data
    assert claim["fact_type"] == mail_source.CLAIMED_TYPE and claim["locator"]["mail"]["scenario"] is True
    assert len(env.rows("SELECT 1 FROM facts WHERE fact_id NOT LIKE 'held:%%'")) == 3


def test_mailed_copy_does_not_load_a_portal_envelope(env):
    pdf = make_pdf(["Old confirmation"])
    claim = {"fact_type": "offer", "subject": {"offer_no": "SYN"}, "attribute": "quantity", "value": "7"}
    envelope = {"source": {"path": "portal/old.pdf", "sha256": sha(pdf)}, "doc_kind": "offer", "spec_version": "1",
                "extracted_by": "w1", "facts": [claim]}
    (env.settings.structured_dir / "docs" / f"{sha(pdf)}.json").write_text(json.dumps(envelope))
    env.deliver(TEST_ADDR, ("old.pdf", pdf))
    assert "portal/old.pdf" not in env.documents() and env.rows("SELECT 1 FROM current_facts") == []


@pytest.mark.parametrize("startup", [False, True])
def test_portal_file_reusing_mail_facts_loses_the_mail_label(env, monkeypatch, startup):
    env.deliver(TEST_ADDR, ("profile.pdf", pdf := make_pdf(["Profile"])), llm=FakeLLM("supplier"))
    path, read = f"portal/{uuid.uuid4().hex}.pdf", lambda *_: pdf
    monkeypatch.setattr(consumer, "_current_sha", lambda *_: sha(pdf))
    monkeypatch.setattr(consumer, "current_sha_by_path", lambda *_: {path: sha(pdf)})
    if startup:   # stopped before it follows the feed, so only the start-up sync runs
        consumer._follow(env.conn, env.conn, env.settings, read, None, env.source, Mock(is_set=lambda: True), 0)
    else:
        consumer.handle_event(env.conn, {"kind": "added", "path": path, "new_sha": sha(pdf)}, read, env.settings, {})
    (fact,) = env.rows("SELECT fact_type, locator FROM current_facts WHERE path = %s", path)
    assert fact["fact_type"] == "supplier" and "mail" not in fact["locator"]
    assert [e["fact_types"] for e in env.rows("SELECT * FROM fact_events WHERE path = %s", path)] == [["supplier"]]


def test_attachments_with_a_taken_name_do_not_replace_each_other(env):
    env.deliver(SUPPLIER_ADDR, ("scan.pdf", make_pdf(["First"])), ("scan.pdf", make_pdf(["Second"])),
                (BODY_FILENAME, b"From: somebody else\nnot the body\n"), llm=FakeLLM())
    assert len(env.documents()) == 4 and len(env.rows("SELECT 1 FROM current_facts")) == 4
    assert env.catch_up() is None   # none of them is processed again
    changed = len(env.rows("SELECT 1 FROM fact_events"))
    for event in env.mail.execute("SELECT payload FROM events WHERE kind = 'document_added'").fetchall():
        env.emit(event["payload"])
    assert env.catch_up(FakeLLM()) is None   # nor when its event is delivered twice
    assert len(env.rows("SELECT 1 FROM fact_events")) == changed == 4


@pytest.mark.parametrize("copies, blob_late", [(2, False), (3, False), (3, True)])
def test_same_attachment_twice_is_one_document(env, copies, blob_late):
    table = b"code,contact_email\nPARTNER-A,a@x.example\nPARTNER-B,b@x.example\n"
    env.receive(SUPPLIER_ADDR, *[("master.csv", table)] * copies)
    if blob_late:
        objects, env.source.objects = env.source.objects, env.mail_dir / "absent"
        env.catch_up()
        env.source.objects = objects
    env.catch_up()
    assert sorted(d["status"] for d in env.documents().values()) == ["parsed", "queued"] and env.catch_up() is None
    assert len(env.rows("SELECT 1 FROM current_facts")) == 2 and len(env.rows("SELECT 1 FROM fact_events")) == 1


def test_mail_cannot_design_kinds_or_maps(env):
    queries = ("SELECT * FROM doc_kinds ORDER BY name", "SELECT * FROM schema_maps")
    registry, llm = [env.rows(q) for q in queries], FakeLLM(kind=None)
    env.deliver(TEST_ADDR, ("new.csv", b"id,email\nPARTNER-B,intruder@elsewhere.example\n"), llm=llm)
    assert [env.rows(q) for q in queries] == registry
    assert sorted(d["status"] for d in env.documents().values()) == ["needs_schema_map", "queued"]
    # An email no kind fits is a visible issue and leaves the queue, so it cannot hold up the portal's documents.
    assert [q["state"] for q in env.rows("SELECT state FROM extraction_queue")] == ["failed"]
    assert env.rows("SELECT 1 FROM issues WHERE kind = 'extraction_failed' AND state = 'open'")
    assert env.catch_up(llm) is None and llm.classified == 1
    client = mail_source.LabellingClient(llm, env.source, env.conn)   # the portal's documents may still be designed
    assert client.design(SimpleNamespace(path="portal/a.txt"), [], {}).target == "doc_kind"


def test_event_committed_below_the_cursor_is_still_read(env):
    put_blob(env.blobs, data := b"From: partner\nlate commit\n")
    with mail_db.connect(URL, env.mail_schema) as late:   # commits on leaving the block
        mail_db.emit(late, "document_added", {"path": "mail/LATE/a/note.txt", "sha256": sha(data), "trust": "test"})
        env.receive()               # takes a higher id and commits first
        env.catch_up()
    env.catch_up(FakeLLM())
    assert "sent_at" not in env.rows("SELECT locator FROM current_facts WHERE path ~ 'LATE'")[0]["locator"]["mail"]


def test_missing_blob_is_visible_and_retried_when_it_arrives(env):
    data, path = b"From: partner\narrives late\n", "mail/X/a/note.txt"
    env.emit({"path": path, "sha256": sha(data), "trust": "supplier_contact", "thread_id": "t"})
    assert env.catch_up() and env.documents()[path]["status"] == "failed"
    assert env.rows("SELECT 1 FROM issues WHERE kind = 'ingest_failed' AND state = 'open'")
    assert env.catch_up() is None   # no busy retry while it is missing
    put_blob(env.blobs, data)
    assert env.catch_up() and env.documents()[path]["status"] == "queued"
    corrupt = sha(b"expected")
    (env.blobs.objects / corrupt[:2]).mkdir(exist_ok=True)
    (env.blobs.objects / corrupt[:2] / corrupt).write_bytes(b"different")
    env.emit({"path": "mail/X/a/bad.txt", "sha256": corrupt, "trust": "supplier_contact"})
    env.deliver()   # a blob that does not match its hash fails without blocking the next document
    assert sorted(d["status"] for d in env.documents().values()) == ["failed", "queued", "queued"]


@pytest.mark.parametrize("change", [{"trust": "unknown_sender"}, {"path": "portal/a.txt"},
                                    {"path": "mail/../portal/a.txt"}, {"sha256": "../../x"}])
def test_untrusted_or_unsafe_event_is_ignored(env, change):
    event = env.emit({"path": "mail/X/a/a.txt", "sha256": sha(b"a"), "trust": "supplier_contact", **change})
    env.catch_up()
    assert env.documents() == {} and env.cursor() == event


def test_lost_connection_does_not_move_the_cursor(env, monkeypatch):
    env.receive()
    monkeypatch.setattr(mail_source, "process_file", Mock(side_effect=psycopg.OperationalError("lost")))
    with pytest.raises(psycopg.OperationalError):
        env.catch_up()
    env.conn.rollback()
    assert env.cursor() == 0


def test_no_mail_schema_and_no_mail_directory_is_fine(env, tmp_path):
    source = mail_source.MailSource(f"nomail_{uuid.uuid4().hex[:6]}", absent := tmp_path / "absent")
    assert source.catch_up(env.conn, env.settings.structured_dir, {}) == [] and not absent.exists()
