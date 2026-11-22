"""`python -m kb sync` and `python -m kb extract` on a database that holds supplier mail (synthetic data, fake LLM)."""

import dataclasses
import shutil
from unittest.mock import Mock

import psycopg
import pytest

from conftest import make_pdf
from kb import __main__ as cli, consumer, facts as facts_mod, ingest, mail_source
from kb.ingest import FileReader
from mail import config as mail_config, contacts
from mail.store import put_blob
from test_kb_mail_source import PERSONA, SUPPLIER_ADDR, TEST_ADDR, URL, Env, FakeLLM, sha
from tracker.store import BlobStore

pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")
MAIL_FACTS = "SELECT fact_id, fact_type, locator FROM current_facts WHERE starts_with(path, 'mail/') ORDER BY 1"


@pytest.fixture
def env(tmp_path, monkeypatch):
    created = Env(tmp_path)
    monkeypatch.setenv("MAIL_SCHEMA", created.mail_schema)
    monkeypatch.setenv("MAIL_DATA_DIR", str(created.mail_dir))
    monkeypatch.setattr(cli, "load_settings", lambda: created.settings)
    yield created
    created.close()


@pytest.fixture
def portal(env, monkeypatch):
    """Sets the tracker's current file set (the public tables are shared by all tests)."""
    def current(files: dict[str, bytes]) -> None:
        for data in files.values():
            put_blob(BlobStore(env.settings.data_dir), data)
        shas = {path: sha(data) for path, data in files.items()}
        for module in (consumer, cli):
            monkeypatch.setattr(module, "current_sha_by_path", lambda *_: shas)

    return current


@pytest.fixture
def extract(monkeypatch):
    def run(llm: FakeLLM) -> None:
        monkeypatch.setattr(cli, "_adapter", lambda spec: llm)
        assert cli.main(["extract"]) == 0

    return run


@pytest.mark.parametrize("command", [["sync"], ["sync", "--force"]], ids=" ".join)
def test_sync_keeps_mail_and_still_removes_portal_files(env, portal, command):
    env.deliver(SUPPLIER_ADDR, ("note.pdf", make_pdf(["Confirmed"])), llm=FakeLLM())
    facts_mod.upsert_document(env.conn, "portal/gone.txt", sha(b"gone"), container="text", status="loaded")
    env.conn.commit()
    portal({})
    documents, facts = env.documents(), env.rows(MAIL_FACTS)
    assert cli.main(command) == 0
    assert env.documents().keys() == documents.keys() - {"portal/gone.txt"}
    assert len(facts) == 2 and env.rows(MAIL_FACTS) == facts
    assert env.catch_up() is None


def test_sync_current_leaves_mail_alone_whoever_calls_it(env):
    env.deliver(llm=FakeLLM())
    (path,) = before = env.documents()
    read = Mock(side_effect=AssertionError("a mail path is never read from the portal"))
    for current in ({}, {path: sha(b"a portal file that claims a mail path")}):
        assert ingest.sync_current(env.conn, current, read, env.settings.structured_dir, {}) == {}
        assert env.documents() == before and len(env.rows(MAIL_FACTS)) == 1


@pytest.mark.parametrize("command", [["sync"], ["sync", "--force"]], ids=" ".join)
def test_portal_table_does_not_invalidate_mail_with_the_same_bytes(env, portal, command):
    table = b"code,contact_email\nPARTNER-C,office@partner.example.net\n"
    env.deliver(SUPPLIER_ADDR, ("list.txt", table), llm=FakeLLM())   # text in the email, a table in the portal
    portal({"portal/list.csv": table})
    facts = env.rows(MAIL_FACTS)
    assert cli.main(command) == 0
    assert len(facts) == 2 and env.rows(MAIL_FACTS) == facts
    portal_facts = env.rows("SELECT fact_type FROM current_facts WHERE path = 'portal/list.csv'")
    assert [f["fact_type"] for f in portal_facts] == ["supplier"]


@pytest.mark.parametrize("known", [False, True], ids=["new", "known"])
def test_sync_announces_stored_types_for_a_copy_of_a_mailed_file(env, portal, known):
    env.deliver(TEST_ADDR, ("profile.pdf", pdf := make_pdf(["Profile"])), llm=FakeLLM("supplier"))
    path = "portal/profile.pdf"
    if known:   # kb knew the file before the email had facts, so only --force reads it again
        facts_mod.upsert_document(env.conn, path, sha(pdf), container="pdf", status="queued")
        env.conn.commit()
    portal({path: pdf})
    assert cli.main(["sync", "--force"] if known else ["sync"]) == 0
    (fact,) = env.rows("SELECT fact_type, locator FROM current_facts WHERE path = %s", path)
    assert fact["fact_type"] == "supplier" and "mail" not in fact["locator"]
    assert [e["fact_types"] for e in env.rows("SELECT * FROM fact_events WHERE path = %s", path)] == [["supplier"]]


def test_sync_does_not_record_a_lost_connection_as_a_failed_file(env):
    lost = Mock(side_effect=psycopg.OperationalError("lost"))
    with pytest.raises(psycopg.OperationalError):
        ingest.sync_current(env.conn, {"portal/a.txt": sha(b"a")}, lambda *_: b"a", env.settings.structured_dir, {},
                            process=lost)
    env.conn.rollback()
    assert env.documents() == {}


@pytest.mark.parametrize("in_portal_store", [False, True], ids=["mail store only", "same bytes in portal"])
def test_extract_reads_mail_and_keeps_the_senders_label(env, extract, in_portal_store):
    result = env.deliver(TEST_ADDR, ("profile.pdf", make_pdf(["Profile"])))
    if in_portal_store:
        shutil.copytree(env.blobs.objects, env.settings.data_dir / "objects", dirs_exist_ok=True)
    extract(FakeLLM("supplier"))   # the fake claims supplier master data from a supplier_contact
    assert len(facts := env.rows(MAIL_FACTS)) == 2
    for fact in facts:
        label = fact["locator"]["mail"]
        assert fact["fact_type"] == mail_source.CLAIMED_TYPE
        assert label["trust"] == "test" and label["scenario"] is True
        assert label["persona_id"] == PERSONA and label["thread_id"] == result.thread_id
    assert {t for e in env.rows("SELECT fact_types FROM fact_events") for t in e["fact_types"]} == {
        mail_source.CLAIMED_TYPE}
    assert not env.rows("SELECT 1 FROM fact_types WHERE name = 'x_from_sender'")
    settings = dataclasses.replace(mail_config.load_settings(load_files=False), kb_schema=env.kb_schema)
    assert contacts.from_kb(env.conn, settings) is None


@pytest.mark.parametrize("in_portal_store", [False, True], ids=["mail store only", "same bytes in portal"])
def test_extract_cannot_design_from_mail_and_parks_what_fits_no_kind(env, extract, in_portal_store):
    env.deliver(TEST_ADDR)
    if in_portal_store:
        shutil.copytree(env.blobs.objects, env.settings.data_dir / "objects", dirs_exist_ok=True)
    kinds = env.rows("SELECT * FROM doc_kinds ORDER BY name")
    llm = FakeLLM(kind=None)
    extract(llm)
    assert env.rows("SELECT * FROM doc_kinds ORDER BY name") == kinds
    assert [q["state"] for q in env.rows("SELECT state FROM extraction_queue")] == ["failed"]
    assert env.rows("SELECT 1 FROM issues WHERE kind = 'extraction_failed' AND state = 'open'")
    extract(llm)
    assert llm.classified == 1


def test_extract_still_reads_and_designs_for_portal_files(env, extract):
    data = b"a portal memo"
    put_blob(BlobStore(env.settings.data_dir), data)
    ingest.process_file(env.conn, "portal/memo.txt", sha(data), FileReader(env.settings.data_dir)(sha(data), ""),
                        env.settings.structured_dir)
    env.conn.commit()
    extract(FakeLLM("supplier", kind=None))
    (fact,) = env.rows("SELECT fact_type, locator FROM current_facts")
    assert fact["fact_type"] == "supplier" and fact["locator"] == {"page": 1}
