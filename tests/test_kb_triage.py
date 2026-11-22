"""Triage tools and replay path. All values are synthetic and no database is needed."""

from agents.llm import ReplayClient

from kb import triage


class FakeConn:
    def __init__(self):
        self.calls = []
        self.documents = [{"path": "misc/note.txt", "sha256": "sha-note", "container": "text",
                           "doc_kind": "other", "status": "queued"}]

    def execute(self, query, params=()):
        self.calls.append((query, params))
        if "FROM documents WHERE active" in query and "doc_kind IS NULL" in query:
            return Rows(self.documents)
        if "FROM documents WHERE path" in query:
            return Rows(self.documents)
        if "FROM facts WHERE" in query:
            return Rows([])
        return Rows([])


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


def context(conn=None):
    conn = conn or FakeConn()
    return triage.TriageContext(conn, lambda sha, path: b"alias ALPHA means canonical A", 0.4,
                                "misc/note.txt", "sha-note")


def test_registry_exposes_only_the_five_triage_tools():
    assert triage.make_registry(context()).names() == ["read_document", "query_facts", "propose_alias",
                                                       "emit_fact", "open_issue"]


def test_emit_fact_clamps_confidence_and_uses_triage_extractor(monkeypatch):
    seen = []
    monkeypatch.setattr(triage.facts_mod, "insert_facts", lambda conn, facts: seen.extend(facts))
    out = context().emit_fact(fact_type="statement", subject={"ref": "A"}, attribute="status", value="open",
                              evidence="status open", confidence=0.95)
    assert out["extractor"] == "triage-agent" and out["confidence"] == 0.4
    assert seen[0]["extractor"] == "triage-agent" and seen[0]["confidence"] == 0.4
    assert seen[0]["source_path"] == "misc/note.txt" and seen[0]["source_sha256"] == "sha-note"


def test_alias_proposal_is_capped_and_does_not_claim_settlement():
    conn = FakeConn()
    out = context(conn).propose_alias(alias_kind="part", alias="ALPHA", canonical_id="A")
    assert out == {"proposed": True, "alias_kind": "part", "alias": "ALPHA", "canonical_id": "A",
                   "confidence": 0.4}
    query, params = next((q, p) for q, p in conn.calls if "INSERT INTO aliases" in q)
    assert params[-1] == 0.4 and "least(aliases.confidence" in query


def test_replay_agent_reads_and_emits_through_bound_tools(monkeypatch):
    emitted = []
    monkeypatch.setattr(triage.facts_mod, "insert_facts", lambda conn, facts: emitted.extend(facts))
    client = ReplayClient([
        {"stop_reason": "tool_use", "tool_calls": [{"name": "read_document", "input": {"path": "misc/note.txt"}}]},
        {"stop_reason": "tool_use", "tool_calls": [{"name": "emit_fact", "input": {
            "fact_type": "statement", "subject": {"ref": "A"}, "attribute": "status", "value": "open",
            "evidence": "status open", "confidence": 1.0}}]},
        {"stop_reason": "end_turn", "text": '{"action":"completed","summary":"one fact"}'},
    ])
    result = triage.run_one(client, context(), max_turns=4)
    assert result["ok"] and result["calls"] == ["read_document", "emit_fact"]
    assert emitted[0]["confidence"] == 0.4 and emitted[0]["extractor"] == "triage-agent"


def test_run_only_selects_unclassified_candidates(monkeypatch):
    client = ReplayClient([{ "stop_reason": "end_turn", "text": '{"action":"no_action","summary":"none"}' }])
    result = triage.run(FakeConn(), client, lambda sha, path: b"text", limit=1)
    assert result["processed"] == 1 and result["results"][0]["action"] == "no_action"
