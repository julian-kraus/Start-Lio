"""Unit tests for the agent tool registry and the grounding guard. No database, no model, no key."""

from agents.tools import GroundingGuard, Tool, ToolRegistry, mail_tools


def hedge(p_fail, copies_at_risk, loss_per_copy_eur, commitment_eur):
    exp = p_fail * copies_at_risk * loss_per_copy_eur
    be = commitment_eur / (copies_at_risk * loss_per_copy_eur)
    return {"expected_loss_eur": round(exp, 2), "break_even_p_fail": round(be, 5), "hedge": exp > commitment_eur}


HEDGE_SCHEMA = {"type": "object", "properties": {
    "p_fail": {"type": "number"}, "copies_at_risk": {"type": "integer"},
    "loss_per_copy_eur": {"type": "number"}, "commitment_eur": {"type": "number"}},
    "required": ["p_fail", "copies_at_risk", "loss_per_copy_eur", "commitment_eur"]}


def make_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.add("hedge", "Deterministic hedge rule.", HEDGE_SCHEMA, hedge, grounds_numbers=True)
    return reg


def test_schemas_are_anthropic_tool_format():
    reg = make_registry()
    schemas = reg.schemas()
    assert schemas == [{"name": "hedge", "description": "Deterministic hedge rule.", "input_schema": HEDGE_SCHEMA}]
    assert reg.grounding_names() == {"hedge"}


def test_call_dispatches_and_returns_dict():
    reg = make_registry()
    out = reg.call("hedge", {"p_fail": 0.35, "copies_at_risk": 3100, "loss_per_copy_eur": 90, "commitment_eur": 3500})
    assert out == {"expected_loss_eur": 97650.0, "break_even_p_fail": 0.01254, "hedge": True}


def test_call_never_raises_into_the_agent():
    reg = make_registry()
    assert "error" in reg.call("nope", {})                              # unknown tool
    assert "error" in reg.call("hedge", {"p_fail": 0.1})                # missing arguments
    assert "error" in reg.call("hedge", ["not", "an", "object"])        # not an object

    def boom(**_):
        raise RuntimeError("kaboom")
    reg.add("boom", "raises", {"type": "object", "properties": {}}, boom)
    out = reg.call("boom", {})
    assert "error" in out and "kaboom" in out["error"]


def test_register_rejects_duplicates():
    reg = make_registry()
    try:
        reg.add("hedge", "again", HEDGE_SCHEMA, hedge)
        assert False, "duplicate registration should raise"
    except ValueError:
        pass


class FakeMail:
    def list_contacts(self, supplier_id=None):
        return {"contacts": ["a@example.com"], "supplier_id": supplier_id}

    def send_approved(self, draft_id=None, approval_id=None):   # must never be exposed to a model
        return {"sent": True}


def test_mail_tools_exclude_the_send_path():
    tools = mail_tools(FakeMail())
    names = {t.name for t in tools}
    assert "list_contacts" in names
    assert "send_approved" not in names                        # human-only, never given to a model
    assert all(not t.grounds_numbers for t in tools)
    by_name = {t.name: t for t in tools}
    assert by_name["list_contacts"].fn(supplier_id="SUP-1") == {"contacts": ["a@example.com"], "supplier_id": "SUP-1"}


def test_guard_passes_a_grounded_verdict():
    guard = GroundingGuard({"expected_loss_eur": "expected_loss_eur", "hedge": "hedge"})
    tool_out = hedge(0.35, 3100, 90, 3500)
    verdict = {"hedge": True, "expected_loss_eur": 97650.0, "reason": "ok"}
    assert guard.check(verdict, [tool_out]) == []


def test_guard_flags_a_computed_number():
    guard = GroundingGuard({"expected_loss_eur": "expected_loss_eur"})
    tool_out = hedge(0.35, 3100, 90, 3500)
    problems = guard.check({"expected_loss_eur": 1234.0}, [tool_out])
    assert len(problems) == 1 and "does not match" in problems[0]


def test_guard_flags_a_number_with_no_tool_source():
    guard = GroundingGuard({"expected_loss_eur": "expected_loss_eur"})
    problems = guard.check({"expected_loss_eur": 97650.0}, [])          # the tool was never called
    assert len(problems) == 1 and "no grounding tool" in problems[0]


def test_guard_apply_forces_the_tool_value_in():
    guard = GroundingGuard({"expected_loss_eur": "expected_loss_eur", "hedge": "hedge"})
    tool_out = hedge(0.35, 3100, 90, 3500)
    fixed = guard.apply({"expected_loss_eur": 5.0, "hedge": False, "reason": "mine"}, [tool_out])
    assert fixed["expected_loss_eur"] == 97650.0 and fixed["hedge"] is True and fixed["reason"] == "mine"


def test_guard_ignores_errored_tool_outputs():
    guard = GroundingGuard({"expected_loss_eur": "expected_loss_eur"})
    assert guard.sources([{"error": "boom"}, hedge(0.35, 3100, 90, 3500)]) == hedge(0.35, 3100, 90, 3500)
