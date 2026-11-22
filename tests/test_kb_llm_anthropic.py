"""Unit tests for the Anthropic extraction adapter. No database, no network, no key: a fake transport stands in
for the SDK and recorded replies in tests/fixtures/replay_kb/ stand in for the API. Synthetic data only."""

import json
import sys
import types
from pathlib import Path

import pytest

from kb.llm.anthropic import (DEFAULT_CACHE_DIR, SPEC_VERSION, AnthropicClient, ApiTransport, ExtractionStop, Reply,
                              _coerce_number, _declared_subject_keys, _envelope, _numeric_attrs)
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


def doc(sha="a" * 64, text="Order Confirmation\nquantity 10 000 pcs", path="confirmations/a.pdf"):
    return Document(path, sha, "pdf", text=text)


def reply(obj, stop="end_turn"):
    return Reply(stop, json.dumps(obj), {"input_tokens": 1, "output_tokens": 2})


class FakeTransport:
    def __init__(self, *, result=None, results=None, error=None):
        self.result, self.results, self.error = result, results, error
        self.calls, self.requests = 0, []

    def create(self, request):
        self.calls += 1
        self.requests.append(dict(request))            # snapshot: the adapter may mutate and resend the request
        if self.error:
            raise self.error
        if self.results is not None:
            return self.results.pop(0)
        return self.result


# ---------- pure helpers ----------

def test_coerce_number_only_for_clean_numbers():
    assert _coerce_number("10 000") == 10000 and isinstance(_coerce_number("10 000"), int)
    assert _coerce_number("1 234 567") == 1234567 and _coerce_number("-42") == -42 and _coerce_number("3.5") == 3.5
    for s in ("1999-12-31", "FOB Testport", "AB12345678", "1,200"):   # dates, text, identifiers, comma groups
        assert _coerce_number(s) is None                              # stay text, never guessed into a number


def test_coerce_number_grouping_and_ambiguity():
    # Two or more same-separator groups of three read as one integer; a single group is ambiguous and stays text.
    assert _coerce_number("1.250.000") == 1250000 and isinstance(_coerce_number("1.250.000"), int)
    assert _coerce_number("1,250,000") == 1250000
    for s in ("10.000", "1.250", "1,250", "2 500,5"):     # single group / comma decimal: not guessed
        assert _coerce_number(s) is None
    assert _coerce_number("0.85") == 0.85 and _coerce_number("10.50") == 10.5   # real decimals still parse


def test_coerce_number_never_joins_separate_numbers():
    # Whitespace is read only as well-formed thousands groups, so two adjacent numbers are never joined into one
    # (the old code deleted all whitespace: "5 000 10 000" -> 500010000).
    for s in ("5 000 10 000", "12 5", "500\n450"):        # a bad group or a newline split stays two numbers
        assert _coerce_number(s) is None
    assert _coerce_number("10 000") == 10000 and _coerce_number("250 000") == 250000


def test_numeric_attrs_includes_quantity_named_string_attrs():
    # Scanned quantities typed as string are still parsed by their name; a weight or identifier stays text.
    contract = {"fact_types": [
        {"name": "delivery_note_line", "attributes": {"final_qty": {"type": "string"},
                                                      "printed_qty": {"type": "string"},
                                                      "gross_weight": {"type": "string"}}},
        {"name": "supplier_statement", "attributes": {"quantity": {"type": "string"}}},
        {"name": "stock_batch", "attributes": {"qty": {"type": "number"}, "last_count": {"type": "string"}}}]}
    numeric = _numeric_attrs(contract)
    assert numeric["delivery_note_line"] == {"final_qty", "printed_qty"}      # not gross_weight
    assert numeric["supplier_statement"] == {"quantity"}
    assert numeric["stock_batch"] == {"qty"}                                  # a number type, not last_count
    assert _numeric_attrs({}) == {} and _numeric_attrs({"fact_types": []}) == {}


def test_extract_parses_string_typed_quantity_by_name():
    contract = {"fact_types": [{"name": "delivery_note_line", "attributes": {"final_qty": {"type": "string"}}}]}
    raw = {"facts": [{"fact_type": "delivery_note_line", "subject": {"dn": "D1"}, "attribute": "final_qty",
                      "value": "10 000"}]}
    client = AnthropicClient(transport=FakeTransport(result=reply(raw)), model="m")
    env = client.extract(doc(), Structure("delivery_note_line", "rules"), {}, contract)
    assert env["facts"][0]["value"] == 10000 and env["facts"][0]["evidence"] == "10 000"


@pytest.mark.parametrize("raw", [{}, {"facts": {"not": "a list"}},
                                 {"facts": [{"fact_type": "oc_line", "subject": "P1", "attribute": "quantity"}]}])
def test_bad_extract_shapes_are_not_cached(tmp_path, raw):
    # A missing facts list, facts as an object, or a string subject is refused before caching: each retry is live.
    transport = FakeTransport(result=reply(raw))
    client = AnthropicClient(transport=transport, model="m", store_dir=tmp_path)
    for _ in range(2):
        with pytest.raises(ValueError):
            client.extract(doc(sha="d" * 64), Structure("oc", "r"), {}, CONTRACT)
    assert transport.calls == 2 and not list(tmp_path.glob("*.json"))


def test_page_cap_records_dropped_scans_in_unreadable():
    # More image-only pages than the cap: text-less pages are sent first and the ones that did not fit recorded.
    pages = [("image/png", b"\x89PNG" + bytes([i])) for i in range(5)]
    document = Document("scan.pdf", "a" * 64, "pdf", text=None, images=pages,
                        pages_without_text=[1, 2, 3, 4, 5])
    raw = {"facts": [], "unreadable": []}
    client = AnthropicClient(transport=FakeTransport(result=reply(raw)), model="m", max_pages=2)
    env = client.extract(document, Structure("scan", "rules"), {}, CONTRACT)
    joined = " ".join(env["unreadable"])
    assert "3 of 5 page images" in joined
    assert "page 3" in joined and "page 4" in joined and "page 5" in joined   # the scans that did not fit


def test_text_truncation_is_recorded():
    document = Document("big.pdf", "a" * 64, "pdf", text="x" * 500)
    client = AnthropicClient(transport=FakeTransport(result=reply({"facts": []})), model="m", max_input_chars=100)
    env = client.extract(document, Structure("oc", "rules"), {}, CONTRACT)
    assert any("truncated to 100 of 500" in u for u in env["unreadable"])


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
    # Only `type: number` attributes are parsed; a number-looking identifier under a text attribute stays exact.
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


def test_extract_sends_subject_keys_and_keys_the_cache_on_them(tmp_path):
    # The model is told the subject keys per fact type (so it does not invent its own and miss the reference),
    # and they fold into the cache key so a changed set is a fresh question, not a replay of the old answer.
    t = FakeTransport(results=[reply({"facts": []}), reply({"facts": []})])
    client = AnthropicClient(transport=t, model="m", store_dir=tmp_path)
    c1 = {"fact_types": [{"name": "oc_line", "subject_keys": ["po_number", "po_line"], "attributes": {}}]}
    client.extract(doc(sha="1" * 64), Structure("oc", "r"), {}, c1)
    sent = " ".join(b.get("text", "") for b in t.requests[0]["messages"][0]["content"])
    assert "subject_keys" in sent and "po_number" in sent and "po_line" in sent
    c2 = {"fact_types": [{"name": "oc_line", "subject_keys": ["oc_number"], "attributes": {}}]}
    client.extract(doc(sha="1" * 64), Structure("oc", "r"), {}, c2)
    assert t.calls == 2                                       # a changed subject-key set is not served cached
    assert _declared_subject_keys().get("po_line") == ["po_number", "po_line"]   # file fallback for the DB view


def test_extract_cache_pays_once_per_sha(tmp_path):
    transport = FakeTransport(result=reply({"doc_kind": "oc", "facts": []}))
    client = AnthropicClient(transport=transport, model="m", store_dir=tmp_path)
    client.extract(doc(sha="b" * 64), Structure("oc", "r"), {}, {})
    client.extract(doc(sha="b" * 64), Structure("oc", "r"), {}, {})
    assert transport.calls == 1                                         # second read served from the cache file
    assert list(tmp_path.glob(f"extract.m.{'b' * 64}.*.json"))          # named by sha plus an input hash
    client.extract(doc(sha="c" * 64), Structure("oc", "r"), {}, {})
    assert transport.calls == 2                                         # a different document is paid for separately


def test_design_cache_key_separates_sheets_of_one_document(tmp_path):
    # design runs once per unknown sheet (same sha, different rows); the rows fold into the key, so two answers.
    first = reply({"target": "schema_map", "name": "a", "schema_map": {"signature": "A", "fact_type": "a"}})
    second = reply({"target": "schema_map", "name": "b", "schema_map": {"signature": "B", "fact_type": "b"}})
    transport = FakeTransport(results=[first, second])
    client = AnthropicClient(transport=transport, model="m", store_dir=tmp_path)
    sheet_a = client.design(doc(sha="e" * 64), [], {}, sample_rows=[["h1", "h2"], ["1", "2"]])
    sheet_b = client.design(doc(sha="e" * 64), [], {}, sample_rows=[["x1", "x2"], ["9", "8"]])
    assert transport.calls == 2 and sheet_a.schema_map["signature"] == "A"
    assert sheet_b.schema_map["signature"] == "B"                       # not the first sheet's cached map
    again = client.design(doc(sha="e" * 64), [], {}, sample_rows=[["h1", "h2"], ["1", "2"]])
    assert transport.calls == 2 and again.schema_map["signature"] == "A"   # same rows, served from the cache


def test_classify_cache_key_follows_the_kind_list(tmp_path):
    # After a new kind is added the kind list changes, so a cached "none" for the old list is not replayed.
    transport = FakeTransport(results=[reply({"decision": "none", "kind": None}),
                                       reply({"decision": "kind", "kind": "k2"})])
    client = AnthropicClient(transport=transport, model="m", store_dir=tmp_path)
    assert client.classify(doc(sha="7" * 64), [Structure("k1", "r")]).decision == "none"
    hit = client.classify(doc(sha="7" * 64), [Structure("k1", "r"), Structure("k2", "r")])
    assert transport.calls == 2 and hit.kind == "k2"                    # a new kind list is a fresh question


def test_classify_and_design_cache_key_follows_the_path(tmp_path):
    # classify and design carry the path, so two paths with one sha route independently, not share an answer (#67).
    ct = FakeTransport(results=[reply({"decision": "none", "kind": None}), reply({"decision": "kind", "kind": "oc"})])
    cc = AnthropicClient(transport=ct, model="m", store_dir=tmp_path)
    assert cc.classify(doc(sha="9" * 64, path="mail/renamed.pdf"), [Structure("oc", "r")]).decision == "none"
    assert cc.classify(doc(sha="9" * 64, path="portal/order.pdf"), [Structure("oc", "r")]).kind == "oc"  # fresh path
    hit = cc.classify(doc(sha="9" * 64, path="mail/renamed.pdf"), [Structure("oc", "r")])
    assert ct.calls == 2 and hit.decision == "none"                    # two calls, then the mail path served cached
    dt = FakeTransport(results=[reply({"target": "doc_kind", "name": "a"}), reply({"target": "doc_kind", "name": "b"})])
    dc = AnthropicClient(transport=dt, model="m", store_dir=tmp_path)
    assert dc.design(doc(sha="8" * 64, path="mail/a.pdf"), [], {}).name == "a"
    assert dc.design(doc(sha="8" * 64, path="portal/a.pdf"), [], {}).name == "b" and dt.calls == 2


def test_default_cache_dir_is_on_and_follows_the_writable_volume(monkeypatch, tmp_path):
    # Without KB_LLM_CACHE_DIR the cache is still on at a git-ignored default; in a container /app is root-owned,
    # so when TRACKER_DATA_DIR points at the writable volume the default cache lives under it instead.
    monkeypatch.delenv("KB_LLM_CACHE_DIR", raising=False)
    monkeypatch.delenv("TRACKER_DATA_DIR", raising=False)
    assert AnthropicClient(transport=FakeTransport(result=reply({"facts": []})), model="m").store_dir \
        == DEFAULT_CACHE_DIR == PROJECT_ROOT / ".cache" / "kb_llm"
    monkeypatch.setenv("TRACKER_DATA_DIR", str(tmp_path / "data"))
    assert AnthropicClient(transport=FakeTransport(result=reply({"facts": []})), model="m").store_dir \
        == tmp_path / "data" / "kb_llm_cache"


def test_unwritable_cache_does_not_lose_the_answer(tmp_path):
    # A read-only cache directory must not discard a paid-for answer: the write is skipped, the answer returns.
    blocked = tmp_path / "ro"
    blocked.write_text("not a directory")                              # mkdir under a file raises, and is caught
    transport = FakeTransport(result=reply({"doc_kind": "oc", "facts": []}))
    client = AnthropicClient(transport=transport, model="m", store_dir=blocked / "sub")
    env = client.extract(doc(sha="b" * 64), Structure("oc", "r"), {}, {})
    assert env["doc_kind"] == "oc" and transport.calls == 1            # answer returned despite no cache write


def test_bad_reply_is_not_cached(tmp_path):
    # A non-JSON reply must not be written to the cache, or every retry would replay the broken answer.
    transport = FakeTransport(result=Reply("end_turn", "not json at all", {}))
    client = AnthropicClient(transport=transport, model="m", store_dir=tmp_path)
    for _ in range(2):
        with pytest.raises(ValueError):
            client.extract(doc(sha="d" * 64), Structure("oc", "r"), {}, {})
    assert transport.calls == 2                                         # retried live, not served a cached failure
    assert not list(tmp_path.glob("*.json"))


@pytest.mark.parametrize("stop", ["refusal", "model_context_window_exceeded"])
def test_hard_stops_raise(stop):
    client = AnthropicClient(transport=FakeTransport(result=reply({"facts": []}, stop=stop)), model="m")
    with pytest.raises(ExtractionStop) as exc:
        client.extract(doc(), Structure("oc", "r"), {}, {})
    assert stop in str(exc.value)


def test_max_tokens_retries_once_with_a_larger_budget():
    # A long scan can overrun the output cap: the first max_tokens is retried once at a larger budget, a second fails.
    ok = FakeTransport(results=[reply({"facts": []}, stop="max_tokens"), reply({"facts": []})])
    client = AnthropicClient(transport=ok, model="m", max_output_tokens=100)
    client.extract(doc(), Structure("oc", "r"), {}, {})
    assert ok.calls == 2 and ok.requests[0]["max_tokens"] == 100 and ok.requests[1]["max_tokens"] > 100
    stuck = FakeTransport(result=reply({"facts": []}, stop="max_tokens"))
    with pytest.raises(ExtractionStop) as exc:
        AnthropicClient(transport=stuck, model="m").extract(doc(sha="e" * 64), Structure("oc", "r"), {}, {})
    assert stuck.calls == 2 and "max_tokens" in str(exc.value)


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
    contract = {"fact_types": [{"name": "oc_line", "subject_keys": ["po_number", "po_line"],
                                "attributes": {"quantity": {"type": "number"}}}]}
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
