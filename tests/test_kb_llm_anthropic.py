"""Unit tests for the Anthropic extraction adapter. No database, no network, no key: a fake transport stands in
for the SDK and recorded replies in tests/fixtures/replay_kb/ stand in for the API. Synthetic data only."""

import json
import sys
import types
from pathlib import Path

import pytest

from kb.llm.anthropic import (DEFAULT_CACHE_DIR, SPEC_VERSION, AnthropicClient, ApiTransport, ExtractionStop, Reply,
                              _coerce_number, _envelope, _numeric_attrs)
from kb.llm.base import Document, Structure, normalise
from tracker.config import PROJECT_ROOT

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "replay_kb"
REPLAY_SHA = "f" * 64

# A small slice of the real contract shape: quantity is numeric, the rest is text. Synthetic throughout.
CONTRACT = {"fact_types": [
    {"name": "oc_line", "attributes": {"quantity": {"type": "number"}, "item_no": {"type": "string"}}},
    {"name": "oc_header", "attributes": {"incoterm": {"type": "string"}, "oc_date": {"type": "string"}}},
]}


@pytest.fixture(autouse=True)
def _never_touch_the_key(tmp_path, monkeypatch):
    """No unit test may read the key file or write the cache into the repo: stub the env loader and point the
    default cache at a throwaway directory. One test deletes KB_LLM_CACHE_DIR to check the real default."""
    monkeypatch.setattr("kb.llm.anthropic.load_env", lambda *a, **k: None)
    monkeypatch.setenv("KB_LLM_CACHE_DIR", str(tmp_path / "default_cache"))
    monkeypatch.delenv("LOOKAHEAD_ANTHROPIC_API_KEY", raising=False)


def doc(sha="a" * 64, text="Order Confirmation\nquantity 10 000 pcs"):
    return Document("confirmations/a.pdf", sha, "pdf", text=text)


def reply(obj, stop="end_turn"):
    return Reply(stop, json.dumps(obj), {"input_tokens": 1, "output_tokens": 2})


class FakeTransport:
    def __init__(self, *, result=None, results=None, error=None):
        self.result, self.results, self.error = result, results, error
        self.calls, self.requests = 0, []

    def create(self, request):
        self.calls += 1
        self.requests.append(request)
        if self.error:
            raise self.error
        if self.results is not None:
            return self.results.pop(0)
        return self.result


# ---------- pure helpers ----------

def test_coerce_number_only_for_clean_numbers():
    assert _coerce_number("10 000") == 10000 and isinstance(_coerce_number("10 000"), int)
    assert _coerce_number("1 234 567") == 1234567
    assert _coerce_number("-42") == -42
    assert _coerce_number("3.5") == 3.5
    assert _coerce_number("1999-12-31") is None         # a date must stay text
    assert _coerce_number("FOB Testport") is None
    assert _coerce_number("AB12345678") is None         # an identifier must stay text
    assert _coerce_number("1,200") is None              # comma grouping is left verbatim, not guessed


def test_coerce_number_grouping_and_ambiguity():
    # Unambiguous thousands groups (two or more same-separator groups of three) read as one integer.
    assert _coerce_number("1.250.000") == 1250000 and isinstance(_coerce_number("1.250.000"), int)
    assert _coerce_number("1,250,000") == 1250000 and isinstance(_coerce_number("1,250,000"), int)
    # A single group is ambiguous with a decimal, so it stays text instead of a wrong number.
    assert _coerce_number("10.000") is None            # not 10.0
    assert _coerce_number("1.250") is None             # not 1.25
    assert _coerce_number("1,250") is None
    assert _coerce_number("2 500,5") is None           # comma decimal is left verbatim, not guessed
    # Real decimals whose fraction is not three digits are still parsed.
    assert _coerce_number("3.5") == 3.5
    assert _coerce_number("0.85") == 0.85
    assert _coerce_number("10.50") == 10.5


def test_envelope_whitelists_keys_and_sets_defaults():
    raw = {"doc_kind": "oc", "facts": [], "junk": "drop me", "extracted_by": "model should not win"}
    env = _envelope(raw, "fallback_kind")
    assert set(env) == {"source", "doc_kind", "doc_kind_confidence", "languages", "spec_version", "extracted_by",
                        "doc_date", "facts", "unreadable", "proposed_fact_types"}
    assert env["source"] == {} and env["extracted_by"] == "" and env["spec_version"] == SPEC_VERSION
    assert env["doc_kind"] == "oc" and _envelope({"facts": []}, "fallback_kind")["doc_kind"] == "fallback_kind"


# ---------- extract ----------

def test_extract_coerces_quantities_and_keeps_text():
    raw = {"doc_kind": "oc", "doc_kind_confidence": "high", "languages": ["en"], "doc_date": "2026-01-15",
           "facts": [{"fact_type": "oc_line", "subject": {"po": "P1"}, "attribute": "quantity", "value": "10 000"},
                     {"fact_type": "oc_header", "subject": {"po": "P1"}, "attribute": "incoterm",
                      "value": "FOB Testport"},
                     {"fact_type": "oc_header", "subject": {"po": "P1"}, "attribute": "oc_date",
                      "value": "1999-12-31"}],
           "unreadable": [], "proposed_fact_types": [], "junk": "drop"}
    client = AnthropicClient(transport=FakeTransport(result=reply(raw)), model="m")
    env = client.extract(doc(), Structure("oc", "rules"), {}, CONTRACT)
    assert "junk" not in env
    qty, incoterm, date = env["facts"]
    assert qty["value"] == 10000 and qty["evidence"] == "10 000"       # verbatim kept as evidence, parsed in code
    assert incoterm["value"] == "FOB Testport" and date["value"] == "1999-12-31"


def test_extract_keeps_identifiers_and_text_attributes_verbatim():
    # Only attributes the contract declares `type: number` are parsed. A number-looking identifier under a text
    # attribute keeps its exact string, including a leading zero, and is never retyped to an int.
    raw = {"facts": [
        {"fact_type": "oc_line", "subject": {"po": "P1"}, "attribute": "item_no", "value": "007"},
        {"fact_type": "oc_header", "subject": {"po": "P1"}, "attribute": "incoterm", "value": "12 34"},
        {"fact_type": "oc_line", "subject": {"po": "P1"}, "attribute": "quantity", "value": "500"},
        {"fact_type": "x_new", "subject": {"po": "P1"}, "attribute": "whatever", "value": "42"}]}
    client = AnthropicClient(transport=FakeTransport(result=reply(raw)), model="m")
    env = client.extract(doc(), Structure("oc", "rules"), {}, CONTRACT)
    item_no, incoterm, qty, proposed = env["facts"]
    assert item_no["value"] == "007"                 # a leading-zero identifier is not retyped
    assert incoterm["value"] == "12 34"              # a text attribute keeps its exact string
    assert qty["value"] == 500                        # a numeric attribute is parsed
    assert proposed["value"] == "42"                 # a proposed fact type not in the contract stays text


def test_numeric_attrs_reads_number_types_from_contract():
    numeric = _numeric_attrs(CONTRACT)
    assert numeric["oc_line"] == {"quantity"} and numeric["oc_header"] == set()
    assert _numeric_attrs({}) == {} and _numeric_attrs({"fact_types": []}) == {}


def test_extract_cache_pays_once_per_sha(tmp_path):
    transport = FakeTransport(result=reply({"doc_kind": "oc", "facts": []}))
    client = AnthropicClient(transport=transport, model="m", store_dir=tmp_path)
    client.extract(doc(sha="b" * 64), Structure("oc", "r"), {}, {})
    client.extract(doc(sha="b" * 64), Structure("oc", "r"), {}, {})
    assert transport.calls == 1                                         # second read served from the cache file
    assert (tmp_path / f"extract.m.{'b' * 64}.json").is_file()
    client.extract(doc(sha="c" * 64), Structure("oc", "r"), {}, {})
    assert transport.calls == 2                                         # a different document is paid for separately


def test_cache_is_on_by_default(monkeypatch):
    # Without KB_LLM_CACHE_DIR the store falls back to a git-ignored default, so a document is still paid once.
    monkeypatch.delenv("KB_LLM_CACHE_DIR", raising=False)
    client = AnthropicClient(transport=FakeTransport(result=reply({"facts": []})), model="m")
    assert client.store_dir == DEFAULT_CACHE_DIR == PROJECT_ROOT / ".cache" / "kb_llm"


def test_bad_reply_is_not_cached(tmp_path):
    # A non-JSON reply must not be written to the cache, or every retry would replay the broken answer.
    transport = FakeTransport(result=Reply("end_turn", "not json at all", {}))
    client = AnthropicClient(transport=transport, model="m", store_dir=tmp_path)
    for _ in range(2):
        with pytest.raises(ValueError):
            client.extract(doc(sha="d" * 64), Structure("oc", "r"), {}, {})
    assert transport.calls == 2                                         # retried live, not served a cached failure
    assert not list(tmp_path.glob("*.json"))


@pytest.mark.parametrize("stop", ["max_tokens", "refusal", "model_context_window_exceeded"])
def test_hard_stops_raise(stop):
    client = AnthropicClient(transport=FakeTransport(result=reply({"facts": []}, stop=stop)), model="m")
    with pytest.raises(ExtractionStop) as exc:
        client.extract(doc(), Structure("oc", "r"), {}, {})
    assert stop in str(exc.value)


def test_missing_reply_without_key_fails_clearly(tmp_path, monkeypatch):
    monkeypatch.setattr("kb.llm.anthropic.load_env", lambda *a, **k: None)
    monkeypatch.delenv("LOOKAHEAD_ANTHROPIC_API_KEY", raising=False)
    client = AnthropicClient(transport=None, api_key=None, store_dir=tmp_path, model="m")
    with pytest.raises(ExtractionStop):
        client.extract(doc(), Structure("oc", "r"), {}, {})


# ---------- classify and design ----------

def test_classify_reads_a_decision():
    raw = {"decision": "kind", "kind": "order_confirmation", "reason": "header matches"}
    client = AnthropicClient(transport=FakeTransport(result=reply(raw)), model="m")
    routing = client.classify(doc(), [Structure("order_confirmation", "rules")])
    assert routing.decision == "kind" and routing.kind == "order_confirmation" and routing.by == client.label
    bad = AnthropicClient(transport=FakeTransport(result=reply({"decision": "???", "kind": "x"})), model="m")
    routing = bad.classify(doc(sha="9" * 64), [])
    assert routing.decision == "none" and routing.kind is None


def test_design_reads_a_schema_map():
    raw = {"target": "schema_map", "name": "ship table", "reason": "new layout",
           "schema_map": {"signature": "sig", "fact_type": "ship"}}
    client = AnthropicClient(transport=FakeTransport(result=reply(raw)), model="m")
    proposal = client.design(doc(), [], {}, sample_rows=[["a", "b"]])
    assert proposal.target == "schema_map" and proposal.schema_map["fact_type"] == "ship"


# ---------- page rendering (scans) ----------

def _one_page_pdf() -> bytes:
    import io

    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_normalise_renders_pdf_pages_to_image_blocks():
    # A fresh install must be able to turn a PDF into the page images the adapter sends for scans. This needs
    # pypdfium2 and Pillow, both pinned in requirements.txt.
    document = normalise("scan.pdf", "e" * 64, "pdf", _one_page_pdf(), render=True)
    assert document.images, "a rendered PDF yields at least one page image"
    media_type, data = document.images[0]
    assert media_type == "image/png" and data.startswith(b"\x89PNG")


# ---------- recorded replay fixtures (offline, no transport) ----------

def test_replay_fixtures_serve_every_role():
    client = AnthropicClient(store_dir=FIXTURES, transport=None, api_key=None, model="replay")
    structure = Structure("order_confirmation", "Read each order confirmation line.")
    contract = {"fact_types": [{"name": "oc_line", "attributes": {"quantity": {"type": "number"}}}]}
    env = client.extract(doc(sha=REPLAY_SHA), structure, {}, contract)
    assert env["facts"][0]["value"] == 10000 and env["doc_kind"] == "order_confirmation"
    assert client.classify(doc(sha=REPLAY_SHA), [structure]).decision == "kind"
    assert client.design(doc(sha=REPLAY_SHA), [], {}).name == "x_test_notice"


# ---------- the live transport (fake SDK module) ----------

def _fake_anthropic(create):
    mod = types.ModuleType("anthropic")

    class APITimeoutError(Exception):
        pass

    class Anthropic:
        def __init__(self, *a, **k):
            self.messages = types.SimpleNamespace(create=create)

    mod.APITimeoutError = APITimeoutError
    mod.Anthropic = Anthropic
    return mod


def test_api_transport_parses_message(monkeypatch):
    block = types.SimpleNamespace(type="text", text='{"facts": []}')
    message = types.SimpleNamespace(stop_reason="end_turn", content=[block],
                                    usage=types.SimpleNamespace(input_tokens=11, output_tokens=22))
    monkeypatch.setitem(sys.modules, "anthropic", _fake_anthropic(lambda **r: message))
    out = ApiTransport("key", 5.0).create({"model": "m", "max_tokens": 10, "system": "s", "messages": []})
    assert out.stop_reason == "end_turn" and out.text == '{"facts": []}'
    assert out.usage == {"input_tokens": 11, "output_tokens": 22}


def test_api_transport_maps_timeout(monkeypatch):
    fake = _fake_anthropic(None)

    def boom(**r):
        raise fake.APITimeoutError("slow")

    fake.Anthropic.__init__ = lambda self, *a, **k: setattr(self, "messages", types.SimpleNamespace(create=boom))
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    with pytest.raises(ExtractionStop) as exc:
        ApiTransport("key", 1.0).create({"messages": []})
    assert "timeout" in str(exc.value)
