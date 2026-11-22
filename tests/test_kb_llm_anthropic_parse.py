"""Answer handling and cache keys of the Anthropic adapter, for the 07:49 UTC review of PR #56. No database, no
network, no key. Synthetic values only."""

import json

import pytest

from kb.llm.anthropic import AnthropicClient, Reply, _coerce_number, _envelope
from kb.llm.base import Document, Structure

CONTRACT = {"fact_types": [
    {"name": "delivery_note_line", "attributes": {"quantity": {"type": "string"}, "unit_price": {"type": "number"}}},
]}
NUMERIC = {"delivery_note_line": {"quantity", "unit_price"}}


@pytest.fixture(autouse=True)
def _never_touch_the_key(tmp_path, monkeypatch):
    monkeypatch.setattr("kb.llm.anthropic.load_env", lambda *a, **k: None)
    monkeypatch.setenv("KB_LLM_CACHE_DIR", str(tmp_path / "default_cache"))
    monkeypatch.delenv("LOOKAHEAD_ANTHROPIC_API_KEY", raising=False)


def fact(value, attribute="quantity", subject=None):
    return {"fact_type": "delivery_note_line", "subject": subject or {"delivery_note": "DN-X1", "line": 1},
            "attribute": attribute, "value": value}


class Transport:
    def __init__(self, answer):
        self.answer, self.calls = answer, 0

    def create(self, request):
        self.calls += 1
        return Reply("end_turn", json.dumps(self.answer), {"input_tokens": 1, "output_tokens": 1})


def doc(text="Delivery note\nquantity 98 765"):
    return Document("receiving/a.pdf", "b" * 64, "pdf", text=text)


# ---------- 1. a single thousands group in a count is thousands ----------

@pytest.mark.parametrize("raw", ["98.765", "98,765"])
def test_count_with_one_thousands_group_is_read_as_thousands(raw):
    assert _coerce_number(raw, count=True) == 98765
    env = _envelope({"facts": [fact(raw)]}, "delivery_note_scan", NUMERIC)
    assert env["facts"][0]["value"] == 98765 and env["facts"][0]["evidence"] == raw


def test_one_group_outside_a_count_stays_ambiguous():
    assert _coerce_number("98.765") is None
    env = _envelope({"facts": [fact("98.765", attribute="unit_price")]}, "delivery_note_scan", NUMERIC)
    assert env["facts"][0]["value"] == "98.765"


def test_unreadable_count_goes_to_review_not_through_as_text():
    env = _envelope({"facts": [fact("98,76")]}, "delivery_note_scan", NUMERIC)
    f = env["facts"][0]
    assert f["confidence"] == "low" and "needs review" in f["note"]
    assert any("quantity" in u for u in env["unreadable"])


def test_a_word_in_a_count_is_left_alone():
    env = _envelope({"facts": [fact("see attached list")]}, "delivery_note_scan", NUMERIC)
    assert env["facts"][0]["value"] == "see attached list" and "confidence" not in env["facts"][0]


# ---------- 3. subject values in the types the reference uses ----------

def test_line_subjects_become_integers_and_ids_stay_text():
    subject = {"delivery_note": "0042", "line": "3", "po_number": "0071", "po_line": "20"}
    env = _envelope({"facts": [fact("5", subject=subject)]}, "delivery_note_scan", NUMERIC)
    assert env["facts"][0]["subject"] == {"delivery_note": "0042", "line": 3, "po_number": "0071", "po_line": 20}


# ---------- 4. the cache key covers the request actually sent ----------

def _extract(tmp_path, transport, rules="Read each line.", **caps):
    client = AnthropicClient(store_dir=tmp_path, transport=transport, model="m", **caps)
    return client.extract(doc(), Structure("delivery_note_scan", rules), {}, CONTRACT)


def test_changed_reading_rules_are_a_new_call(tmp_path):
    t = Transport({"facts": [fact("5")]})
    _extract(tmp_path, t, rules="Read each line.")
    _extract(tmp_path, t, rules="Read each line and the footer.")
    assert t.calls == 2


def test_raising_the_text_cap_is_a_new_call_not_a_stale_partial_read(tmp_path):
    t = Transport({"facts": [fact("5")]})
    first = _extract(tmp_path, t, max_input_chars=10)
    assert any("truncated" in u for u in first["unreadable"])
    second = _extract(tmp_path, t, max_input_chars=100_000)
    assert t.calls == 2 and not second["unreadable"]


def test_same_request_is_still_paid_once(tmp_path):
    t = Transport({"facts": [fact("5")]})
    _extract(tmp_path, t)
    _extract(tmp_path, t)
    assert t.calls == 1


# ---------- 5. a malformed fact is never cached ----------

@pytest.mark.parametrize("bad", [{"attribute": ["quantity"]}, {"fact_type": ["delivery_note_line"]}])
def test_fact_with_non_string_type_or_attribute_is_refused_before_caching(tmp_path, bad):
    t = Transport({"facts": [{**fact("5"), **bad}]})
    for _ in range(2):
        with pytest.raises(ValueError):
            _extract(tmp_path, t)
    assert t.calls == 2, "each retry is a fresh call, not a cached bad answer"
    assert not list(tmp_path.glob("extract.*"))


# ---------- po_line is the ERP line index, not the printed item number ----------

def test_po_line_copied_from_the_printed_item_number_becomes_the_line_index():
    subject = {"po_number": "PO-X1", "po_line": "30"}
    facts = [{"fact_type": "oc_line", "subject": dict(subject), "attribute": a, "value": v}
             for a, v in (("item_no_printed", "00030"), ("qty", "5"))]
    env = _envelope({"facts": facts}, "order_confirmation", {"oc_line": {"qty"}})
    assert {f["subject"]["po_line"] for f in env["facts"]} == {3}


def test_po_line_that_already_differs_from_the_printed_item_is_kept():
    facts = [{"fact_type": "oc_line", "subject": {"po_number": "PO-X1", "po_line": 3}, "attribute": "item_no_printed",
              "value": "00030"}]
    env = _envelope({"facts": facts}, "order_confirmation", {})
    assert env["facts"][0]["subject"]["po_line"] == 3
