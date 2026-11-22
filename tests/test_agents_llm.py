"""The agent loop against the replay adapter: the validator guard, its retries, and the fall back to the
deterministic tool result. Plus the Anthropic adapter's key refusal and request shape (no network), and that a
run and its calls are written to plan.agent_traces (throwaway Postgres only). Synthetic numbers only."""

import os
import uuid
from pathlib import Path

import pytest
from psycopg import sql

from agents import llm
from agents.tools import GroundingGuard, ToolRegistry
from agents.traces import Tracer

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "replay"
URL = os.environ.get("TEST_DATABASE_URL")

VERDICT_SCHEMA = {"type": "object", "properties": {
    "hedge": {"type": "boolean"}, "expected_loss_eur": {"type": "number"},
    "break_even_p_fail": {"type": "number"}, "reason": {"type": "string"}}}
GUARD = GroundingGuard({"hedge": "hedge", "expected_loss_eur": "expected_loss_eur",
                        "break_even_p_fail": "break_even_p_fail"})
SYSTEM = "You decide whether to place a backup order. Every number must come from a tool."
PROMPT = "Should we hedge the backup offer for the premium edition?"


def hedge(p_fail, copies_at_risk, loss_per_copy_eur, commitment_eur):
    exp = p_fail * copies_at_risk * loss_per_copy_eur
    be = commitment_eur / (copies_at_risk * loss_per_copy_eur)
    return {"expected_loss_eur": round(exp, 2), "break_even_p_fail": round(be, 5), "hedge": exp > commitment_eur}


def make_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.add("hedge", "Deterministic hedge rule.",
            {"type": "object", "properties": {"p_fail": {"type": "number"}}}, hedge, grounds_numbers=True)
    return reg


def client_for(name: str) -> llm.ReplayClient:
    return llm.ReplayClient(FIXTURES / f"{name}.json")


def run(name: str, tracer: Tracer | None = None) -> llm.AgentResult:
    return llm.run(client_for(name), make_registry(), role="hedger", system=SYSTEM, prompt=PROMPT,
                   schema=VERDICT_SCHEMA, guard=GUARD, tracer=tracer)


def test_grounded_answer_passes_the_guard():
    res = run("hedge_grounded")
    assert res.ok
    assert res.calls == ["hedge"]
    assert res.guard_retries == 0 and res.guard_failed is False
    assert res.verdict["expected_loss_eur"] == 97650.0 and res.verdict["hedge"] is True


def test_computed_answer_is_rejected_then_the_retry_is_grounded():
    res = run("hedge_reject_then_fixed")
    assert res.ok
    assert res.guard_retries == 1 and res.guard_failed is False
    assert res.calls == ["hedge"]                                   # the tool was only called on the retry
    assert res.verdict["expected_loss_eur"] == 97650.0


def test_deterministic_result_wins_when_the_model_will_not_align():
    res = run("hedge_stubborn")
    assert res.ok                                                  # a run that falls back still returns an answer
    assert res.guard_retries == 2 and res.guard_failed is True
    assert res.verdict["expected_loss_eur"] == 97650.0            # the tool value, not the model's 5.0
    assert res.verdict["reason"] == "not changing it"            # non-numeric fields are kept


def test_a_verdict_with_no_tool_result_is_refused_not_passed():
    # the model never calls a tool and answers three times with its own number: nothing grounds it.
    invented = '{"hedge": true, "expected_loss_eur": 5.0, "reason": "my own sum"}'
    res = llm.run(llm.ReplayClient([{"stop_reason": "end_turn", "text": invented}] * 3), make_registry(),
                  role="hedger", system=SYSTEM, prompt=PROMPT, schema=VERDICT_SCHEMA, guard=GUARD)
    assert not res.ok and "no grounding tool result" in res.error
    assert res.guard_failed is True and res.calls == []
    assert "expected_loss_eur" not in res.verdict and "hedge" not in res.verdict   # the invented values are gone


def test_an_exhausted_script_ends_the_run_with_an_error():
    # only a tool-use turn, no final answer: the loop asks for another reply and the script runs out.
    res = llm.run(llm.ReplayClient([{"stop_reason": "tool_use", "tool_calls": [{"name": "hedge", "input": {}}]}]),
                  make_registry(), role="hedger", system=SYSTEM, prompt=PROMPT, schema=VERDICT_SCHEMA, guard=GUARD)
    assert not res.ok and "exhausted" in res.error


def test_anthropic_refuses_to_start_without_a_key(monkeypatch):
    monkeypatch.setattr(llm, "load_env", lambda *_a, **_k: None)    # no secrets file may inject a key
    monkeypatch.delenv("LOOKAHEAD_ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="refusing to start"):
        llm.AnthropicClient()


def test_anthropic_builds_a_request_with_schema_and_tools():
    seen = {}

    class FakeTransport:
        def create(self, request):
            seen.update(request)
            return llm.AgentReply("end_turn", text='{"hedge": true}')

    client = llm.AnthropicClient(transport=FakeTransport(), api_key=None, model="claude-haiku-5-5")
    reply = client.message(system="sys", messages=[{"role": "user", "content": "hi"}],
                           tools=make_registry().schemas(), schema=VERDICT_SCHEMA)
    assert reply.stop_reason == "end_turn"
    assert seen["model"] == "claude-haiku-5-5"
    assert seen["output_config"]["format"] == {"type": "json_schema", "schema": VERDICT_SCHEMA}
    assert seen["tools"][0]["name"] == "hedge"
    assert "max_tokens" in seen


# ---- traces written to the plan schema ---------------------------------------

@pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")
def test_run_and_calls_are_written_to_plan_agent_traces(monkeypatch):
    from plan import clock, db

    monkeypatch.delenv(clock.ENV_VAR, raising=False)
    schema = f"plan_test_{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("PLAN_SCHEMA", schema)
    conn = db.connect(URL, schema)
    try:
        db.migrate(conn, schema)
        conn.commit()
        tracer = Tracer(conn, role="hedger", kind="agent", input={"prompt": PROMPT})
        res = run("hedge_grounded", tracer=tracer)
        assert res.ok and res.run_id is not None

        run_row = conn.execute(sql.SQL("SELECT status, scenario, output FROM {} WHERE run_id = %s")
                               .format(sql.Identifier("runs")), (res.run_id,)).fetchone()
        assert run_row["status"] == "done" and run_row["scenario"] is False
        assert run_row["output"]["expected_loss_eur"] == 97650.0

        rows = tracer.rows()
        tool_rows = [r for r in rows if r["tool"] == "hedge"]
        assert len(tool_rows) == 1
        assert tool_rows[0]["output"]["expected_loss_eur"] == 97650.0
        assert tool_rows[0]["input"] == {"p_fail": 0.35, "copies_at_risk": 3100,
                                         "loss_per_copy_eur": 90, "commitment_eur": 3500}
        verdict_rows = [r for r in rows if r["verdict_ok"] is True]
        assert len(verdict_rows) == 1 and verdict_rows[0]["output"]["hedge"] is True
        assert all(r["model"] == "replay" for r in rows)
    finally:
        conn.rollback()
        conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
        conn.commit()
        conn.close()
