"""plan/contract: schemas are valid, examples validate, and the Lookahead record kinds match tracker/lookahead.py."""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from plan import contracts
from tracker import lookahead, web

ROOT = Path(__file__).resolve().parent.parent
REHEARSAL = ROOT / "tests" / "fixtures" / "lookahead_rehearsal.jsonl"
PLAN_CONTRACTS = ["coverage", "weak_spot", "offer", "promise", "recommendation", "decision", "timeline_event"]


def _jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_every_schema_is_valid_draft_2020_12():
    for name in contracts.names():
        schema = json.loads((contracts.CONTRACT_DIR / f"{name}.schema.json").read_text())
        Draft202012Validator.check_schema(schema)
        assert schema["$id"].endswith(f"/{name}.schema.json")


def test_required_contracts_exist():
    assert set(PLAN_CONTRACTS) <= set(contracts.names())


def test_record_kinds_match_tracker():
    assert set(contracts.record_kinds()) == lookahead.KINDS


@pytest.mark.parametrize("name", PLAN_CONTRACTS)
def test_example_validates(name):
    obj = json.loads((contracts.EXAMPLES_DIR / f"{name}.json").read_text())
    contracts.validate(name, obj)


def test_example_records_validate_and_fold():
    records = _jsonl(contracts.EXAMPLES_DIR / "lookahead_records.jsonl")
    assert {r["kind"] for r in records} == lookahead.KINDS
    for r in records:
        contracts.validate_record(r)
        lookahead.validate(r)
    state = lookahead.fold([{**r, "id": i} for i, r in enumerate(records, 1)])
    assert state["plan"]["coverage"]["needed"] == 1000


def test_rehearsal_fixture_validates():
    records = _jsonl(REHEARSAL)
    assert records
    for n, r in enumerate(records, 1):
        errs = contracts.errors("record_" + r["kind"], r)
        assert not errs, f"line {n}: {errs}"


def test_recommendation_needs_hedge_inputs():
    obj = json.loads((contracts.EXAMPLES_DIR / "recommendation.json").read_text())
    del obj["hedge"]["p_fail"]
    with pytest.raises(contracts.ContractError, match="p_fail"):
        contracts.validate("recommendation", obj)
    obj = json.loads((contracts.EXAMPLES_DIR / "recommendation.json").read_text())
    obj["hedge"]["p_fail"] = 1.5
    with pytest.raises(contracts.ContractError):
        contracts.validate("recommendation", obj)


def test_bad_records_are_refused():
    with pytest.raises(contracts.ContractError, match="unknown record kind"):
        contracts.validate_record({"kind": "nope"})
    with pytest.raises(contracts.ContractError, match="supplier_id"):
        contracts.validate_record({"kind": "supplier"})
    with pytest.raises(contracts.ContractError):
        contracts.validate_record({"kind": "event", "type": "document"})
    with pytest.raises(contracts.ContractError):
        contracts.validate_record({"kind": "launch", "mode": "sometimes"})
    with pytest.raises(contracts.ContractError):
        contracts.validate("no_such_contract", {})


def test_extra_fields_are_allowed():
    contracts.validate_record({"kind": "cue", "label": "x", "added_later": 1})


def _example(name):
    return json.loads((contracts.EXAMPLES_DIR / f"{name}.json").read_text())


@pytest.mark.parametrize("with_goal_answer", [True, False])
def test_records_written_by_the_app_validate(tmp_path, with_goal_answer):
    """The human steps write records that the rehearsal fixture does not contain: play it through the app."""
    la = lookahead.Lookahead(tmp_path / "log.jsonl", REHEARSAL)
    assert la.state()["mode"] == "rehearse"   # accept only touches mail in live mode
    questions = la.state()["launch"]["goal_questions"]
    answers = {q["id"]: q["suggested"] for q in questions if with_goal_answer or q["id"] != "goal"}
    while True:
        status = la.state()["rehearsal"]
        waits = la._last_cue().get("wait_for") if status["blocked_by"] else None
        if waits == "goal":
            web.lookahead_goal(la, {"answers": answers})
        elif waits and waits.startswith("accept:"):
            web.lookahead_accept(la, waits.partition(":")[2], {"approver": "Test Person"})
        elif status["applied"] < status["beats"]:
            la.advance()
        else:
            break
    written = _jsonl(tmp_path / "log.jsonl")
    assert any(r["kind"] == "goal" for r in written) and any(r.get("decided_by") for r in written)
    for n, r in enumerate(written, 1):
        errs = contracts.errors("record_" + r["kind"], r)
        assert not errs, f"record {n} ({r['kind']}): {errs}"


@pytest.mark.parametrize("field,value", [("answers", "text"), ("budget_eur", "5000"), ("budget_eur", -1)])
def test_bad_goal_fields_are_refused(field, value):
    with pytest.raises(contracts.ContractError, match=field):
        contracts.validate_record({"kind": "goal", field: value})


@pytest.mark.parametrize("value", [
    "tomorrow", "", "2030-01-10", "2030-01-10T08:00:00", "2030-01-10 08:00:00+01:00", "20300110T080000+0100",
    "2030-02-30T08:00:00+00:00", "2030-01-10T25:00:00Z", "2030-01-10T08:00:00+0100", "2030-01-10T08:00:00+01:60",
    "2030-01-10T08:00:00+24:00", "2030-01-10T08:00:00Z\n",
])
def test_bad_and_naive_times_are_refused(value):
    obj = {**_example("recommendation"), "decide_by": value}
    with pytest.raises(contracts.ContractError, match="decide_by"):
        contracts.validate("recommendation", obj)
    with pytest.raises(contracts.ContractError, match="now"):
        contracts.validate_record({"kind": "clock", "now": value})


@pytest.mark.parametrize("value", [
    "2030-01-10T08:00:00Z", "2030-01-10t08:00:00z", "2030-01-10T08:00:00+01:00", "2028-02-29T23:59:59.123456-05:30",
    "2030-01-10T08:00:00.1234567Z",
])
def test_rfc3339_times_are_accepted(value):
    contracts.validate_record({"kind": "clock", "now": value})


def test_every_format_in_the_schemas_is_checked():
    def formats(node):
        if isinstance(node, dict):
            if isinstance(node.get("format"), str):
                yield node["format"]
            for v in node.values():
                yield from formats(v)
        elif isinstance(node, list):
            for v in node:
                yield from formats(v)
    for name in contracts.names():
        checker = contracts.validator(name).format_checker
        assert set(formats(contracts.validator(name).schema)) <= set(checker.checkers), name


@pytest.mark.parametrize("kind", ["hedge", "buffer"])
def test_recommendation_kind_needs_its_inputs(kind):
    obj = {**_example("recommendation"), "kind": kind}
    contracts.validate("recommendation", obj)
    del obj[kind]
    with pytest.raises(contracts.ContractError, match=f"'{kind}' is a required property"):
        contracts.validate("recommendation", obj)


@pytest.mark.parametrize("kind", ["expedite", "substitute", "hold"])
def test_other_recommendations_need_no_model_inputs(kind):
    obj = {k: v for k, v in _example("recommendation").items() if k not in ("hedge", "buffer")}
    contracts.validate("recommendation", {**obj, "kind": kind})


@pytest.mark.parametrize("name", ["offer", "recommendation", "weak_spot"])
def test_required_sources_cannot_be_empty(name):
    with pytest.raises(contracts.ContractError, match="sources"):
        contracts.validate(name, {**_example(name), "sources": []})


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_numbers_must_be_finite(value):
    obj = _example("recommendation")
    obj["hedge"]["p_fail"] = value
    with pytest.raises(contracts.ContractError, match="p_fail"):
        contracts.validate("recommendation", obj)
    with pytest.raises(contracts.ContractError, match="budget_eur"):
        contracts.validate_record({"kind": "goal", "budget_eur": value})
    contracts.validate_record({"kind": "goal", "budget_eur": 10 ** 400})   # a huge integer is finite
