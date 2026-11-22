"""`kb run` rounds in which a queued document leaves the queue without facts (synthetic data, fake LLM)."""

import pytest

from kb import ingest, reconcile
from mail.store import put_blob
from test_kb_mail_source import URL, Env, FakeLLM, sha
from tracker.store import BlobStore

pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")
OPEN = "SELECT kind FROM issues WHERE state = 'open' AND payload->>'path' = %s ORDER BY kind"
PATH = "portal/note.txt"


class Unreadable(FakeLLM):
    def extract(self, document, structure, schema, contract):
        raise ValueError("no answer")


class FailsGate(FakeLLM):
    def extract(self, document, structure, schema, contract):
        envelope = super().extract(document, structure, schema, contract)
        envelope["facts"][0]["subject"] = {"supplier_id": "row-3"}
        return envelope


@pytest.fixture
def env(tmp_path):
    created = Env(tmp_path)
    data = b"a portal note"
    put_blob(BlobStore(created.settings.data_dir), data)
    ingest.process_file(created.conn, PATH, sha(data), data, created.settings.structured_dir)
    created.conn.commit()
    yield created
    created.close()


def open_issues(env) -> list[str]:
    return [r["kind"] for r in env.rows(OPEN, PATH)]


def show_as_pending(env) -> None:
    reconcile.run(env.conn, "test")
    assert open_issues(env) == ["pending_extraction"]


def test_the_attempt_that_fails_a_document_reconciles(env):
    show_as_pending(env)
    llm = Unreadable()
    for _ in range(2):
        assert env.catch_up(llm) is None   # a retry changes no state
        assert open_issues(env) == ["pending_extraction"]
    result = env.catch_up(llm)
    assert result is not None
    assert open_issues(env) == ["extraction_failed"]
    (run,) = env.rows("SELECT detail FROM runs WHERE run_id = %s", result["run_id"])
    assert run["detail"]["paths"] == [{"path": PATH, "status": "failed"}]
    assert env.catch_up(llm) is None   # and only that round does


def test_a_document_that_fails_the_gate_reconciles(env):
    show_as_pending(env)
    llm = FailsGate()
    result = env.catch_up(llm)
    assert result is not None
    assert open_issues(env) == ["review_needed"]
    (run,) = env.rows("SELECT detail FROM runs WHERE run_id = %s", result["run_id"])
    assert run["detail"]["paths"] == [{"path": PATH, "status": "review"}]
    assert env.catch_up(llm) is None
