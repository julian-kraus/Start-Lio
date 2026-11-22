"""plan/contract: schemas are valid, examples validate, and the Lookahead record kinds match tracker/lookahead.py."""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from plan import contracts
from tracker import lookahead

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
