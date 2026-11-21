"""Survey, validation gate, live intake through a fake adapter, designer, promotion and the extraction eval.
Unit tests need nothing; the database tests run like tests/test_kb_integration.py. Synthetic data only."""

import hashlib
import json
import os
import uuid

import pytest
from psycopg import sql

from conftest import make_pdf
from kb import db, eval_extraction, intake, reconcile, registry, rules, survey, validate
from kb.facts import load_valid_facts
from kb.ingest import process_file
from kb.llm.base import LLMClient, Routing, Structure, StructureProposal
from kb.llm.replay import ReplayClient
from kb.tabular import signature

URL = os.environ.get("TEST_DATABASE_URL")
if not URL and os.environ.get("KB_TEST_USE_DEV_DB") == "1":
    from kb.config import load_settings
    URL = load_settings().database_url
needs_db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL (or KB_TEST_USE_DEV_DB=1) not set")

KIND_MD = """```yaml
recognise:
  path_globs: ["confirmations/*.pdf"]
  any_phrases: ["Order Confirmation"]
  container: pdf
checks:
  - {fact_type: ship, sum: [shipped, balance], equals: ordered}
```
Read every item line.
"""


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------- unit ----------

def test_parse_recognise_and_route():
    rec = registry.parse_recognise(KIND_MD)
    assert rec["path_globs"] == ["confirmations/*.pdf"] and rec["checks"][0]["equals"] == "ordered"
    kinds = [Structure("oc", KIND_MD, rec), Structure("other", "x", {"path_globs": ["notices/*"]})]
    r = survey.route_document("confirmations/a.pdf", "pdf", "... order confirmation ...", kinds)
    assert r.decision == "kind" and r.kind == "oc" and "path matches" in r.reason
    assert survey.route_document("confirmations/a.pdf", "pdf", "invoice", kinds).decision == "none"
    both = kinds + [Structure("oc2", "", {"path_globs": ["confirmations/*"]}, "core")]
    assert survey.route_document("confirmations/a.pdf", "pdf", "Order Confirmation", both).decision == "ambiguous"


def test_gate():
    env = {"facts": [{"fact_type": "ship", "subject": {"ref": "A"}, "attribute": "ordered", "value": 500},
                     {"fact_type": "ship", "subject": {"ref": "A"}, "attribute": "shipped", "value": 300},
                     {"fact_type": "ship", "subject": {"ref": "A"}, "attribute": "balance", "value": 100}]}
    text = "ordered 500 shipped 300 balance 100 " + "filler " * 40
    res = validate.check(env, schema=None, known_types={"ship"}, text=text,
                         kind_checks=[{"fact_type": "ship", "sum": ["shipped", "balance"], "equals": "ordered"}])
    assert res.ok and any("check failed" in w for w in res.warnings)
    bad = {"facts": [{"fact_type": "ship", "subject": {"ref": "row-3"}, "attribute": "ordered", "value": 999}]}
    res = validate.check(bad, schema=None, known_types={"ship"}, text=text)
    assert not res.ok and any("row number" in f for f in res.fatal) and any("text layer" in f for f in res.fatal)
    res = validate.check({"facts": [{"fact_type": "made_up", "subject": {"k": "1"}, "attribute": "a", "value": 1}]},
                         schema=None, known_types={"ship"}, text=None)
    assert not res.ok


# ---------- database ----------

class FakeLLM(LLMClient):
    """Deterministic stand-in for a provider: returns what each test tells it to."""
    name = "fake"
    needs_images = False

    def __init__(self, envelopes=None, routing=None, proposal=None):
        self.envelopes, self.routing, self.proposal = envelopes or {}, routing, proposal

    def classify(self, document, kinds):
        return self.routing or Routing("none", None, "nothing fits", "llm:fake")

    def extract(self, document, structure, schema, contract):
        return json.loads(json.dumps(self.envelopes[document.sha256]))

    def design(self, document, similar, contract, sample_rows=None):
        return self.proposal


@pytest.fixture
def env(tmp_path):
    schema = f"kb_test_{uuid.uuid4().hex[:8]}"
    conn = db.connect(URL, schema)
    db.migrate(conn, schema)
    seed = tmp_path / "seed.yaml"
    seed.write_text("""
rules:
  - rule_id: qty
    key: [ref]
    candidates:
      - {label: paper, fact_type: ship, attribute: shipped}
      - {label: plan, fact_type: plan, attribute: shipped}
    compare: number
    decide: preference
    min_confidence: 0.6
    severity: high
settings: {proposed_confidence_cap: 0.5}
""")
    rules.seed(conn, seed)
    registry.upsert_doc_kind(conn, "oc", KIND_MD, origin="test", status="core")
    conn.execute("INSERT INTO fact_types (name, origin) VALUES ('ship', 'test'), ('plan', 'test')")
    conn.commit()
    structured = tmp_path / "structured"
    (structured / "docs").mkdir(parents=True)
    try:
        yield conn, structured
    finally:
        conn.rollback()
        conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        conn.commit()
        conn.close()


def envelope(path, data, facts):
    return {"source": {"path": path, "sha256": sha(data)}, "doc_kind": "oc", "spec_version": "1",
            "extracted_by": "x", "facts": facts}


def reader_for(files):
    return lambda s, p: files[s]


@needs_db
def test_routing_extract_gate_and_review(env):
    conn, structured = env
    good = make_pdf(["Order Confirmation", "ref A ordered 500 shipped 300 balance 200"])
    bad = make_pdf(["Order Confirmation", "ref B ordered 10"])
    assert process_file(conn, "confirmations/good.pdf", sha(good), good, structured) == "queued"
    assert process_file(conn, "confirmations/bad.pdf", sha(bad), bad, structured) == "queued"
    q = conn.execute("SELECT doc_kind, routing FROM extraction_queue WHERE path = 'confirmations/good.pdf'").fetchone()
    assert q["doc_kind"] == "oc" and q["routing"]["by"] == "rules"
    conn.commit()
    ship = lambda ref, a, v: {"fact_type": "ship", "subject": {"ref": ref}, "attribute": a, "value": v,
                              "evidence": str(v), "confidence": "high"}
    llm = FakeLLM({sha(good): envelope("confirmations/good.pdf", good, [ship("A", "ordered", 500),
                                                                        ship("A", "shipped", 300)]),
                   sha(bad): envelope("confirmations/bad.pdf", bad, [{**ship("B", "ordered", 77777),
                                                                      "subject": {"ref": "row-9"}}])})
    counts = intake.drain(conn, llm, reader_for({sha(good): good, sha(bad): bad}), structured,
                          {"high": 0.9, "medium": 0.75, "low": 0.4})
    assert counts["done"] == 1 and counts["needs_review"] == 1
    assert conn.execute("SELECT count(*) AS n FROM facts WHERE source_path = 'confirmations/good.pdf' AND valid "
                        "AND extractor = 'llm:fake'").fetchone()["n"] == 2
    assert conn.execute("SELECT count(*) AS n FROM facts WHERE source_path = 'confirmations/bad.pdf' AND valid"
                        ).fetchone()["n"] == 0
    reconcile.run(conn, "test")
    kinds = {r["kind"] for r in conn.execute("SELECT kind FROM issues WHERE state = 'open'")}
    assert "review_needed" in kinds and "pending_extraction" not in kinds
    assert registry.pending_review(conn)["documents"][0]["path"] == "confirmations/bad.pdf"


@needs_db
def test_hash_reuse_across_paths(env):
    conn, structured = env
    pdf = make_pdf(["Order Confirmation", "ref A shipped 300"])
    (structured / "docs" / f"{sha(pdf)}.json").write_text(json.dumps(envelope("confirmations/a.pdf", pdf, [
        {"fact_type": "ship", "subject": {"ref": "A"}, "attribute": "shipped", "value": 300, "confidence": "high"}])))
    assert process_file(conn, "confirmations/a.pdf", sha(pdf), pdf, structured) == "loaded"
    assert process_file(conn, "archive/a-copy.pdf", sha(pdf), pdf, structured) == "reused"
    row = conn.execute("SELECT checks FROM facts WHERE source_path = 'archive/a-copy.pdf' AND valid").fetchone()
    assert row["checks"]["reused_from"] == "confirmations/a.pdf"


@needs_db
def test_unknown_kind_is_designed_capped_and_promoted(env):
    conn, structured = env
    pdf = make_pdf(["Notice of something new", "ref A shipped 300"])
    plan = b"ref,shipped\nA,500\n"
    conn.execute("INSERT INTO schema_maps (signature, format, mapping, origin) VALUES (%s, 'csv', %s, 'test')",
                 (signature(["ref", "shipped"]), json.dumps({"signature": signature(["ref", "shipped"]),
                  "fact_type": "plan", "subject": ["ref"], "columns": {
                      "ref": {"field": "ref", "type": "text"}, "shipped": {"field": "shipped", "type": "int"}}})))
    assert process_file(conn, "plan.csv", sha(plan), plan, structured) == "parsed"
    assert process_file(conn, "notices/new.pdf", sha(pdf), pdf, structured) == "queued"
    conn.commit()
    llm = FakeLLM({sha(pdf): {"source": {}, "doc_kind": "new_notice", "facts": [
                      {"fact_type": "ship", "subject": {"ref": "A"}, "attribute": "shipped", "value": 300,
                       "confidence": "high"}]}},
                  proposal=StructureProposal("doc_kind", "new_notice", "unseen notice",
                                             description="Read the notice.", recognise={"path_globs": ["notices/*"]}))
    assert intake.drain(conn, llm, reader_for({sha(pdf): pdf}), structured, {"high": 0.9})["done"] == 1
    assert registry.structure(conn, "new_notice").status == "proposed"
    capped = [f for f in load_valid_facts(conn, 0.5) if f.source_path == "notices/new.pdf"]
    assert capped and all(f.confidence <= 0.5 for f in capped)
    reconcile.run(conn, "test")
    believed = conn.execute("SELECT value, winner_label FROM believed WHERE rule_id = 'qty'").fetchone()
    assert believed["value"] == 500, "a proposed kind must not win against a settled source"
    assert conn.execute("SELECT 1 FROM issues WHERE kind = 'promotion_needed' AND state = 'open'").fetchone()

    registry.promote(conn, "doc_kind", "new_notice", "tester")
    conn.commit()
    reconcile.run(conn, "promotion")
    believed = conn.execute("SELECT value, winner_label FROM believed WHERE rule_id = 'qty'").fetchone()
    assert believed["value"] == 300 and believed["winner_label"] == "paper"
    assert conn.execute("SELECT action FROM promotions").fetchone()["action"] == "promote"


@needs_db
def test_table_designer_auto_and_proposed(env):
    conn, structured = env
    csv_good = b"Shipment;Shipped\nA;1.200\n"
    good_map = {"signature": signature(["Shipment", "Shipped"]), "format": "csv", "fact_type": "ship",
                "subject": ["ref"], "columns": {"Shipment": {"field": "ref", "type": "text"},
                                                "Shipped": {"field": "shipped", "type": "int", "thousands_sep": "."}}}
    llm = FakeLLM(proposal=StructureProposal("schema_map", "ship table", "new layout", schema_map=good_map))
    assert process_file(conn, "exports/ship.csv", sha(csv_good), csv_good, structured, llm=llm) == "parsed"
    assert conn.execute("SELECT status FROM schema_maps").fetchone()["status"] == "auto"
    assert conn.execute("SELECT value FROM facts WHERE attribute = 'shipped' AND valid").fetchone()["value"] == 1200

    csv_bad = b"Ref;Qty\nrow-1;abc\n"
    bad_map = {"signature": signature(["Ref", "Qty"]), "format": "csv", "fact_type": "ship", "subject": ["ref"],
               "columns": {"Ref": {"field": "ref", "type": "text"}, "Qty": {"field": "shipped", "type": "int"}}}
    llm = FakeLLM(proposal=StructureProposal("schema_map", "bad", "guess", schema_map=bad_map))
    assert process_file(conn, "exports/bad.csv", sha(csv_bad), csv_bad, structured, llm=llm) == "needs_schema_map"
    row = conn.execute("SELECT status, validation FROM schema_maps WHERE signature = %s",
                       (bad_map["signature"],)).fetchone()
    assert row["status"] == "proposed" and not row["validation"]["ok"]


@needs_db
def test_eval_extraction_with_replay(env):
    conn, structured = env
    pdf = make_pdf(["Order Confirmation"])
    ref = envelope("confirmations/a.pdf", pdf, [{"fact_type": "ship", "subject": {"ref": "A"},
                                                 "attribute": "shipped", "value": 300}])
    (structured / "docs" / f"{sha(pdf)}.json").write_text(json.dumps(ref))
    result = eval_extraction.run(conn, ReplayClient(structured), structured, reader_for({sha(pdf): pdf}),
                                 {"confirmations/a.pdf": sha(pdf)})
    assert result["oc"]["agreement"] == 1.0 and result["oc"]["files"] == 1
    assert conn.execute("SELECT agreement FROM extraction_evals").fetchone()["agreement"] == 1.0
    wrong = FakeLLM({sha(pdf): envelope("confirmations/a.pdf", pdf, [{"fact_type": "ship", "subject": {"ref": "A"},
                                                                      "attribute": "shipped", "value": 30}])})
    result = eval_extraction.run(conn, wrong, structured, reader_for({sha(pdf): pdf}),
                                 {"confirmations/a.pdf": sha(pdf)}, store=False)
    assert result["oc"]["agreement"] == 0.0 and result["oc"]["disagreements"][0]["candidate"] == 30
