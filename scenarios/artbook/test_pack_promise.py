"""A replayed check-in and cancellation, through kb extraction, to the promise ledger."""

import importlib.util
import os
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path

import psycopg
import pytest
from psycopg import conninfo, sql

from agents import broker
from kb import consumer, db as kb_db, rules
from kb.config import KbSettings
from kb.ingest import FileReader
from kb.llm.base import LLMClient, Routing
from kb.loader import load_contract
from kb.mail_source import MailSource
from plan import clock, db as plan_db
from sourcing import promises

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
spec = importlib.util.spec_from_file_location("demo_reset", ROOT / "ops" / "demo" / "reset.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)

URL = os.environ.get("TEST_DATABASE_URL")
VALUES = {"unit_loss": "7", "budget": "300", "part": "printed booklet", "offer_quantity": "250", "offer_price": "2.5",
          "offer_b_price": "2.9"}


class ReplayedAnswers(LLMClient):
    """The statement an extractor gives for each mail of the pack. The extract prompt asks for no `as_of`, so only
    the `dated` answers carry one (the time of the mail's Date header)."""
    name, needs_images = "pack-replay", False

    def __init__(self, dated):
        self.dated = dated

    def classify(self, document, kinds):
        return Routing("kind", "supplier_email", "replayed", self.label)

    def extract(self, document, structure, schema, contract):
        text = document.text
        said = datetime.fromisoformat(re.search(r"(?m)^Date: (.+)$", text)[1].strip()).isoformat()
        kind = "cancel" if "we cancel" in text else "confirm" if "load within" in text else None
        line = re.search(r"(?m)^.*(?:we cancel|load within).*$", text)
        return {"source": {}, "doc_kind": structure.name, "doc_kind_confidence": "high", "languages": ["en"],
                "spec_version": "1", "extracted_by": self.name, "doc_date": said, "unreadable": [],
                "proposed_fact_types": [],
                "facts": [{"fact_type": "supplier_statement", "subject": {"statement_no": "1"},
                           "attribute": attribute, "value": value,
                           **({"as_of": said} if self.dated else {}), "confidence": "high",
                           "locator": {"line": 1}, "evidence": "replayed answer"}
                          for attribute, value in (("promise_kind", kind), ("value", line[0].strip()))] if kind else []}


@pytest.fixture
def demo_db():
    if not URL:
        pytest.skip("TEST_DATABASE_URL not set")
    if conninfo.conninfo_to_dict(URL).get("host") not in demo.LOOPBACK:
        pytest.skip("only a local throwaway Postgres")
    name = f"pack_{uuid.uuid4().hex[:8]}_demo"
    with psycopg.connect(URL, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            yield conninfo.make_conninfo(URL, dbname=name)
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.mark.parametrize("dated", [True, False], ids=["answer_with_as_of", "answer_as_the_prompt_asks"])
def test_the_replayed_cancellation_breaks_the_check_in_promise(demo_db, tmp_path, dated):
    pack, log, blobs = demo.load_pack(HERE), tmp_path / "log", tmp_path / "mail"
    structured, seed = tmp_path / "structured", tmp_path / "seed.yaml"
    shutil.copytree(ROOT / "structured" / "contract", structured / "contract")
    seed.write_text("rules: []\nsettings:\n  confidence_map: {high: 0.9, medium: 0.75, low: 0.4}\n")
    settings = KbSettings(database_url=demo_db, schema="kb", data_dir=tmp_path / "data",
                          structured_dir=structured, seed_path=seed, llm_adapter=None)
    demo.reset(demo_db, pack, "2031-03-09T20:00:00+01:00", VALUES, log)
    source, client = MailSource("mail", blobs), ReplayedAnswers(dated)

    def deliver(beat):
        assert demo.replay(demo_db, pack, beat, log, blobs).new
        source.catch_up(conn, structured, {})
        consumer.extract(conn, settings, FileReader(settings.data_dir), client, source, {"high": 0.9})
        found = broker.read_statements(conn)
        conn.commit()
        return found

    with kb_db.connect(demo_db, "kb") as conn, plan_db.connect(demo_db) as plan:
        kb_db.migrate(conn, "kb")
        rules.seed(conn, seed)
        load_contract(conn, structured)

        [check_in] = deliver("check_in")
        assert check_in.supplier_id == pack["persona"]["persona_id"]
        # The broker's own tool, so the pack's mail has to carry what the tool insists on: supplier, time, duration.
        registry = broker.broker_registry(plan, None, registered=[], statements=[check_in])
        quoted = re.search(r"within \d+ h", check_in.text)[0]
        saved = registry.call("register_promise", {"promise_id": "p", "what": "loads", "fallback": "use the backup",
                                                  "within": quoted, "sources": [check_in.source()]})
        plan.commit()
        if not dated and check_in.at is None:
            assert "no readable time" in saved.get("error", ""), saved
            assert broker.open_promises(plan) == [], "a check-in without a time registers nothing"
            pytest.xfail("#171: kb carries no mail time, so the broker refuses to watch the check-in")
        assert "error" not in saved, saved
        assert broker.apply_ledger(plan, [check_in]) == [], "a promise is not proven by the mail that made it"
        plan.commit()

        clock.set_scenario(plan, "2031-03-09T06:00:00+01:00", rate=0, by="test")
        plan.commit()
        statements = deliver("cancellation")
        settled = broker.apply_ledger(plan, statements)
        assert len(settled) == 1, "the cancellation broke no promise"
        [broken] = settled
        assert broken["state"] == promises.BROKEN and broken["statement_kind"] == "cancel"
