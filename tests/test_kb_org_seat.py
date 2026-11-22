"""org_seat: contract, reading rules and a replay through the extraction adapter. No database, network or key.

The chart and its answer are synthetic and written by hand. The replay proves the path from an answer to stored
fact rows and that the rules reach the model. It does not prove that a model follows the rules: that is the live run.
"""

import json
from pathlib import Path

import pytest

from kb import validate
from kb.llm.anthropic import AnthropicClient, Reply
from kb.llm.base import Document, Structure
from kb.loader import DEFAULT_CONFIDENCE, envelope_facts
from tracker.config import PROJECT_ROOT

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "replay_kb"
CONTRACT_DIR = PROJECT_ROOT / "structured" / "contract"
RULES = (CONTRACT_DIR / "doc_kinds" / "org_chart.md").read_text()
CONTRACT = json.loads((CONTRACT_DIR / "fact_types.json").read_text())
ATTRIBUTES = {"holder", "status", "valid_from", "valid_until", "line_key"}


class Replay:
    def __init__(self):
        self.request = None

    def create(self, request):
        self.request = request
        saved = json.loads((FIXTURES / "org_seat.synthetic.json").read_text())
        return Reply(saved["stop_reason"], saved["text"], saved["usage"])


@pytest.fixture
def replayed(tmp_path, monkeypatch):
    monkeypatch.setattr("kb.llm.anthropic.load_env", lambda *a, **k: None)
    text = (FIXTURES / "org_seat.synthetic.txt").read_text()
    transport = Replay()
    client = AnthropicClient(transport=transport, model="replay", store_dir=tmp_path, record=False)
    env = client.extract(Document("org/synthetic.pdf", "e" * 64, "pdf", text=text),
                         Structure("org_chart", RULES), {}, CONTRACT)
    env["source"] = {"path": "org/synthetic.pdf", "sha256": "e" * 64}
    env["extracted_by"] = client.label
    return env, text, transport.request


def seats_of(env):
    seats = {}
    for f in env["facts"]:
        seats.setdefault((f["subject"]["title"], f["subject"]["scope"]), {})[f["attribute"]] = f["value"]
    return seats


def test_contract_is_the_fixed_interface():
    [seat] = [t for t in CONTRACT["fact_types"] if t["name"] == "org_seat"]
    assert seat["subject_keys"] == ["source_path", "title", "scope"]
    assert set(seat["attributes"]) == ATTRIBUTES
    assert all(a["type"] == "string" for a in seat["attributes"].values())


def test_rules_state_every_field_and_status():
    for word in ("org_seat", "source_path", "title", "scope", *ATTRIBUTES, "held", "vacant", "absent", "unclear"):
        assert f"`{word}`" in RULES
    for wording in ("not filled", "search ongoing", "any language", "retiring", "out of office", "left the company"):
        assert wording in RULES


def test_the_whole_rules_and_the_type_reach_the_model(replayed):
    _, _, request = replayed
    sent = "\n".join(b.get("text", "") for b in request["messages"][0]["content"])
    assert RULES in sent                    # extract cuts reading rules at 4000 characters
    assert '"org_seat"' in sent


def test_only_a_named_holder_is_held(replayed):
    seats = seats_of(replayed[0])
    held = {key: a["holder"] for key, a in seats.items() if a["status"] == "held"}
    assert held == {("Plant Director", "North Site"): "Alex Example", ("Sample Planner", ""): "Casey Sample"}


def test_rules_make_a_printed_start_date_held_from_that_day():
    rules = " ".join(RULES.split())
    assert "with a printed start date is `held`, also when that date is still to come" in rules
    assert "`valid_from` must then be that date" in rules
    assert "a start that is not printed as a full date" in rules    # held without a date would sign too early
    assert "a person who has not started yet, and" not in rules


def test_a_seat_with_a_printed_start_date_keeps_the_date(replayed):
    env, text, _ = replayed
    assert "Casey Sample from 2031-03-10" in text
    seat = seats_of(env)[("Sample Planner", "")]
    assert (seat["status"], seat["holder"], seat["valid_from"]) == ("held", "Casey Sample", "2031-03-10")


@pytest.mark.parametrize("seat, printed, status", [
    (("Buyer", "Metals"), "Not Filled", "vacant"),
    (("Buyer", "Plastics"), "Seat Empty", "vacant"),
    (("Buyer", "Textiles"), "Offene Stelle", "vacant"),
    (("Quality Lead", ""), "Quality Lead\nOut of office", "absent"),
    (("Finance Lead", ""), "Jo James Retiring", "absent"),
    (("Logistics Lead", ""), "Juniper Reach\nLogistics Lead", "unclear"),
])
def test_a_seat_without_a_person_in_it_is_not_held(replayed, seat, printed, status):
    env, text, _ = replayed
    assert printed in text
    got = seats_of(env)[seat]
    assert got["status"] == status
    assert got["holder"] == ("Jo James" if "Jo James" in printed else "")


def test_every_seat_is_complete_grounded_and_stored_as_text(replayed):
    env, text, _ = replayed
    gate = validate.check(env, schema=validate.load_schema(PROJECT_ROOT / "structured"),
                          known_types={t["name"] for t in CONTRACT["fact_types"]}, text=text)
    assert gate.ok and not gate.warnings
    seats = seats_of(env)
    assert len(seats) == 8 and all(set(a) == ATTRIBUTES for a in seats.values())
    rows = envelope_facts(env, DEFAULT_CONFIDENCE)
    assert len(rows) == 8 * len(ATTRIBUTES)          # nothing collapsed or dropped
    for row in rows:
        assert set(row["subject"]) == {"source_path", "title", "scope"}
        assert isinstance(row["value"], str) and row["evidence"] in text
        assert row["source_path"] == "org/synthetic.pdf"
