"""Documents that fit no kind are tried once and do not hold up the queue (synthetic data, fake LLM)."""

from datetime import datetime, timedelta, timezone

import pytest

from kb import intake, reconcile, registry
from kb.ingest import process_file
from kb.llm.base import LLMClient, Routing, StructureProposal
from mail.store import put_blob
from test_kb_intake import env, needs_db, sha  # noqa: F401 (env is a fixture)
from test_kb_mail_source import Env
from tracker.store import BlobStore

pytestmark = needs_db
START = datetime(2030, 1, 1, tzinfo=timezone.utc)


class ExtractOnly(LLMClient):
    name, needs_images = "extract-only", False

    def __init__(self):
        self.extracted = []

    def extract(self, document, structure, schema, contract):
        self.extracted.append(document.path)
        return {"source": {}, "doc_kind": structure.name, "facts": [
            {"fact_type": "ship", "subject": {"ref": document.sha256[:8]}, "attribute": "note", "value": "seen",
             "confidence": "high"}]}


class Classifier(ExtractOnly):
    name = "classifier"

    def __init__(self, kind=None):
        super().__init__()
        self.kind, self.classified = kind, []

    def classify(self, document, kinds):
        self.classified.append(document.path)
        return Routing("kind" if self.kind else "none", self.kind, "nothing fits", self.label)


class Designer(Classifier):
    def __init__(self, error=None):
        super().__init__()
        self.error, self.designed = error, 0

    def design(self, document, similar, contract, sample_rows=None):
        self.designed += 1
        if self.error:
            raise self.error
        return StructureProposal("none", "", "nothing to propose")


@pytest.fixture
def queue(env):  # noqa: F811
    conn, structured = env
    conn.execute("UPDATE doc_kinds SET recognise = %s WHERE name = 'oc'", ('{"path_globs": ["known/*"]}',))
    files = {}

    def add(path):
        data = f"synthetic document {path}".encode()
        assert process_file(conn, path, sha(data), data, structured) == "queued"
        # One minute apart, so the oldest-first order is the order of the calls.
        conn.execute("UPDATE extraction_queue SET enqueued_at = %s WHERE path = %s",
                     (START + timedelta(minutes=len(files)), path))
        files[sha(data)] = data
        conn.commit()

    def drain(llm):
        return intake.drain(conn, llm, lambda digest, _: files[digest], structured, {"high": 0.9})

    return conn, add, drain


def test_no_kind_is_classified_once_and_stays_visible(queue):
    conn, add, drain = queue
    add("unknown/a.txt")
    llm = Classifier()
    assert [drain(llm)["unrouted"] for _ in range(3)] == [1, 0, 0]
    assert llm.classified == ["unknown/a.txt"]
    reconcile.run(conn, "test")
    (issue,) = conn.execute("SELECT kind, title FROM issues WHERE state = 'open'").fetchall()
    assert issue["kind"] == "pending_extraction" and "nothing fits" in issue["title"]


@pytest.mark.parametrize("adapter", [Classifier, ExtractOnly, Designer])
def test_twenty_unrouted_documents_do_not_stop_the_next_one(queue, adapter):
    _, add, drain = queue
    for i in range(20):
        add(f"unknown/{i}.txt")
    add("known/21.txt")
    llm = adapter()
    drain(llm)
    drain(llm)
    assert llm.extracted == ["known/21.txt"]


def test_a_designer_with_no_proposal_is_asked_once(queue):
    _, add, drain = queue
    add("unknown/a.txt")
    llm = Designer()
    for _ in range(3):
        drain(llm)
    assert llm.designed == 1


def test_a_kind_the_registry_does_not_have_is_classified_once(queue):
    _, add, drain = queue
    add("unknown/a.txt")
    llm = Classifier("not_registered")
    for _ in range(3):
        drain(llm)
    assert len(llm.classified) == 1


def test_a_recognised_kind_that_left_the_registry_does_not_stall(queue):
    conn, add, drain = queue
    add("known/a.txt")
    conn.execute("DELETE FROM doc_kinds")
    conn.commit()
    assert [drain(ExtractOnly())["unrouted"] for _ in range(3)] == [1, 0, 0]


@pytest.mark.parametrize("change", ["new", "edited", "rejected"])
def test_a_changed_registry_gives_one_more_try(queue, change):
    conn, add, drain = queue
    add("unknown/a.txt")
    llm = Classifier()
    drain(llm)
    drain(llm)
    if change == "new":
        registry.upsert_doc_kind(conn, "memo", "A memo.", origin="test", status="core")
    elif change == "edited":
        registry.upsert_doc_kind(conn, "oc", "Other reading rules.", origin="test", status="core")
    else:
        registry.promote(conn, "doc_kind", "oc", "tester", reject=True)
    conn.commit()
    drain(llm)
    drain(llm)
    assert len(llm.classified) == 2
    routing = conn.execute("SELECT routing FROM extraction_queue").fetchone()["routing"]
    assert routing["rules"]["by"] == "rules", "a second try keeps the survey's answer, not a nest of attempts"


def test_a_new_kind_gets_the_waiting_document_extracted(queue):
    conn, add, drain = queue
    add("unknown/a.txt")
    llm = Classifier()
    drain(llm)
    registry.upsert_doc_kind(conn, "memo", "A memo.", origin="test", status="core")
    conn.commit()
    llm.kind = "memo"
    assert drain(llm)["done"] == 1
    detail = conn.execute("SELECT detail FROM documents").fetchone()["detail"]
    assert intake.NO_FIT not in detail["routing"]


def test_another_adapter_gives_one_more_try(queue):
    _, add, drain = queue
    add("unknown/a.txt")
    drain(ExtractOnly())
    llm = Classifier()
    drain(llm)
    drain(llm)
    assert len(llm.classified) == 1


def test_new_documents_go_before_second_tries(queue):
    conn, add, drain = queue
    for i in range(20):
        add(f"unknown/{i}.txt")
    llm = Classifier()
    drain(llm)
    registry.upsert_doc_kind(conn, "memo", "A memo.", origin="test", status="core")
    conn.commit()
    add("known/new.txt")
    drain(llm)
    assert llm.extracted == ["known/new.txt"]


def test_a_designer_error_is_a_failed_attempt_and_not_a_missing_kind(queue):
    conn, add, drain = queue
    add("unknown/a.txt")
    llm = Designer(TimeoutError("no answer"))
    assert [drain(llm)["failed"] for _ in range(4)] == [1, 1, 1, 0]
    item = conn.execute("SELECT state, last_error FROM extraction_queue").fetchone()
    assert item["state"] == "failed" and "TimeoutError" in item["last_error"]


def test_kb_run_classifies_once_across_polls(tmp_path):
    worker = Env(tmp_path)
    try:
        path, data = "unknown/memo.txt", b"a synthetic memo"
        put_blob(BlobStore(worker.settings.data_dir), data)
        assert process_file(worker.conn, path, sha(data), data, worker.settings.structured_dir) == "queued"
        worker.conn.commit()
        llm = Classifier()
        for _ in range(3):
            worker.catch_up(llm)
        assert llm.classified == [path]
        assert [q["state"] for q in worker.rows("SELECT state FROM extraction_queue")] == ["pending"]
    finally:
        worker.close()
