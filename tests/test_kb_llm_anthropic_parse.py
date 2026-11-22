"""Answer handling and cache keys of the Anthropic adapter, for the 07:49 UTC review of PR #56. Most tests need no
database, network or key; the one drain test needs TEST_DATABASE_URL. Synthetic values only."""

import json
import os
import uuid

import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from tracker.config import PROJECT_ROOT

from kb.llm.anthropic import AnthropicClient, Reply, _coerce_number, _envelope, _numeric_attrs
from kb.llm.base import Document, Structure

CONTRACT = {"fact_types": [
    {"name": "delivery_note_line", "attributes": {"quantity": {"type": "string"}, "unit_price": {"type": "number"}}},
]}
NUMERIC = {"delivery_note_line": {"quantity", "unit_price"}}

# The real contract, read only, so the quantity-attribute rules are proven against the names the records use.
REAL_CONTRACT = json.loads((PROJECT_ROOT / "structured" / "contract" / "fact_types.json").read_text())
REAL_NUMERIC = _numeric_attrs(REAL_CONTRACT)


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


def test_an_unreadable_count_is_not_emitted_as_a_fact():
    # A count whose text is not one clean number must not pass as a text fact: left in facts it would reach
    # kb.engine.normalise("98,76", "number") and be read as 9876. It is dropped and recorded in unreadable.
    env = _envelope({"facts": [fact("98,76")]}, "delivery_note_scan", NUMERIC)
    assert env["facts"] == []
    assert any("quantity" in u and "not loaded" in u for u in env["unreadable"])


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


# ---------- dates and flags in the form the records use ----------

@pytest.mark.parametrize("raw,iso", [("2031/02/03", "2031-02-03"), ("03.02.2031", "2031-02-03"),
                                     ("23/02/2031", "2031-02-23"), ("2031-2-3", "2031-02-03")])
def test_unambiguous_dates_become_iso(raw, iso):
    env = _envelope({"facts": [fact(raw, attribute="note_date")]}, "delivery_note_scan", NUMERIC)
    assert env["facts"][0]["value"] == iso and env["facts"][0]["evidence"] == raw


@pytest.mark.parametrize("raw", ["03/02/2031", "early February", "31.13.2031"])
def test_ambiguous_or_invalid_dates_stay_as_printed(raw):
    env = _envelope({"facts": [fact(raw, attribute="received_stamp_date")]}, "delivery_note_scan", NUMERIC)
    assert env["facts"][0]["value"] == raw


@pytest.mark.parametrize("raw,flag", [("yes", True), ("No", False), ("x", True)])
def test_flags_become_booleans(raw, flag):
    env = _envelope({"facts": [fact(raw, attribute="receiver_signature_present")]}, "delivery_note_scan", NUMERIC)
    assert env["facts"][0]["value"] is flag


def test_a_flag_word_outside_a_flag_attribute_is_text():
    env = _envelope({"facts": [fact("yes", attribute="remark")]}, "delivery_note_scan", NUMERIC)
    assert env["facts"][0]["value"] == "yes"


# ---------- regressions found on d7feaa9, against the real contract ----------

@pytest.mark.parametrize("value,expected", [("0.125", 0.125), ("0.500", 0.5), ("1.250", "1.250")])
def test_qty_per_ratio_is_never_read_as_thousands(value, expected):
    # bom_line.qty_per is a per-unit ratio, not a count: a single thousands group must never turn "0.125" into
    # 125 or "1.250" into 1250. It reads a leading-zero fraction and leaves the ambiguous "1.250" as text.
    subject = {"parent_id": "P-1", "child_id": "C-1"}
    env = _envelope({"facts": [{"fact_type": "bom_line", "subject": subject, "attribute": "qty_per",
                                "value": value}]}, "bom", REAL_NUMERIC)
    assert env["facts"][0]["value"] == expected


@pytest.mark.parametrize("value,expected", [("0.750", 0.75), ("0.500", 0.5)])
def test_leading_zero_in_a_count_is_never_thousands(value, expected):
    # offer.qty is a count, but a leading zero is never a thousands group: "0.750" is 0.75, never 750.
    subject = {"supplier_id": "S-1", "offer_ref": "O-1", "item": "I-1"}
    env = _envelope({"facts": [{"fact_type": "offer", "subject": subject, "attribute": "qty", "value": value}]},
                    "offer", REAL_NUMERIC)
    assert env["facts"][0]["value"] == expected


def test_po_line_1_to_12_keeps_every_line():
    # A confirmation numbered 1..12 prints its own line index. Item 10 must not be divided to line 1 and overwrite
    # the real line 1: all 12 lines survive and line 1 keeps its own quantity.
    facts = []
    for i in range(1, 13):
        subject = {"po_number": "PO-1", "po_line": i}
        facts.append({"fact_type": "oc_line", "subject": dict(subject), "attribute": "item_no_printed",
                      "value": str(i)})
        facts.append({"fact_type": "oc_line", "subject": dict(subject), "attribute": "qty", "value": str(100 + i)})
    env = _envelope({"facts": facts}, "order_confirmation", REAL_NUMERIC)
    qty = {f["subject"]["po_line"]: f["value"] for f in env["facts"] if f["attribute"] == "qty"}
    assert set(qty) == set(range(1, 13)) and qty[1] == 101


def test_tens_convention_still_divides_the_printed_item():
    # The intended case is unchanged: a confirmation printing 10, 20, 30 maps po_line to the ERP line index.
    facts = [{"fact_type": "oc_line", "subject": {"po_number": "PO-1", "po_line": n}, "attribute": a, "value": v}
             for n in (10, 20, 30) for a, v in (("item_no_printed", str(n)), ("qty", "5"))]
    env = _envelope({"facts": facts}, "order_confirmation", REAL_NUMERIC)
    assert {f["subject"]["po_line"] for f in env["facts"]} == {1, 2, 3}


# ---------- defect 3 proven through the live intake queue ----------

URL = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")


class _Fixed:
    def __init__(self, answer):
        self.answer = answer

    def create(self, request):
        return Reply("end_turn", json.dumps(self.answer), {"input_tokens": 1, "output_tokens": 1})


@needs_db
def test_an_unreadable_count_never_becomes_a_fact_through_drain(tmp_path):
    from conftest import make_pdf
    from kb import db, intake, registry

    schema = f"kb_test_{uuid.uuid4().hex[:8]}"
    conn = db.connect(URL, schema)
    db.migrate(conn, schema)
    try:
        registry.upsert_doc_kind(conn, "dns", "Read each line.", origin="test", status="core")
        conn.execute("INSERT INTO fact_types (name, origin, attributes) VALUES (%s, %s, %s)",
                     ("delivery_note_line", "test", Jsonb({"quantity": {"type": "string"}})))
        pdf = make_pdf(["Delivery note", "quantity 98,76"])
        sha = __import__("hashlib").sha256(pdf).hexdigest()
        conn.execute("INSERT INTO extraction_queue (path, sha256, container, doc_kind, state) "
                     "VALUES (%s, %s, 'pdf', 'dns', 'pending')", ("receiving/a.pdf", sha))
        conn.commit()
        answer = {"facts": [{"fact_type": "delivery_note_line", "subject": {"delivery_note": "DN-1", "line": 1},
                             "attribute": "quantity", "value": "98,76"}]}
        client = AnthropicClient(transport=_Fixed(answer), model="m", store_dir=tmp_path, record=False)
        counts = intake.drain(conn, client, lambda s, p: pdf, tmp_path, {"high": 0.9, "medium": 0.75, "low": 0.4})
        assert counts["failed"] == 0
        rows = conn.execute("SELECT value FROM facts WHERE source_path = 'receiving/a.pdf'").fetchall()
        assert all("98" not in str(r["value"]) for r in rows), "the ambiguous count must never be loaded as a fact"
    finally:
        conn.rollback()
        conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        conn.commit()
        conn.close()


def _lines(env):
    return [f["subject"]["line"] for f in env["facts"]]


def test_code_in_a_line_key_is_renumbered_by_position():
    facts = [fact("5", subject={"delivery_note": "DN-X1", "line": "PRT-AA-01"}),
             fact("6", subject={"delivery_note": "DN-X1", "line": "PRT-BB-02"}),
             fact("7", attribute="unit_price", subject={"delivery_note": "DN-X1", "line": "PRT-AA-01"})]
    env = _envelope({"facts": facts}, "delivery_note_scan", NUMERIC)
    assert _lines(env) == [1, 2, 1]


def test_numeric_or_mixed_line_keys_are_left_alone():
    numbered = [fact("5", subject={"delivery_note": "DN-X1", "line": "3"})]
    assert _lines(_envelope({"facts": numbered}, "delivery_note_scan", NUMERIC)) == [3]
    mixed = [fact("5", subject={"delivery_note": "DN-X1", "line": 2}),
             fact("6", subject={"delivery_note": "DN-X1", "line": "PRT-AA-01"})]
    assert _lines(_envelope({"facts": mixed}, "delivery_note_scan", NUMERIC)) == [2, "PRT-AA-01"]


def test_line_codes_are_numbered_per_delivery_note():
    facts = [fact("5", subject={"delivery_note": "DN-X1", "line": "PRT-AA-01"}),
             fact("6", subject={"delivery_note": "DN-X2", "line": "PRT-CC-09"})]
    assert _lines(_envelope({"facts": facts}, "delivery_note_scan", NUMERIC)) == [1, 1]


def test_extract_prompt_defines_positions_and_asks_for_every_attribute(tmp_path):
    seen = {}

    class Capture:
        def create(self, request):
            seen.update(request)
            return Reply("end_turn", json.dumps({"facts": []}), {"input_tokens": 1, "output_tokens": 1})

    client = AnthropicClient(api_key="test", store_dir=tmp_path, transport=Capture(), record=False)
    doc = Document(path="x/note.txt", sha256="a" * 64, container="txt", text="synthetic")
    client.extract(doc, Structure("delivery_note_scan", "", None, "core"), {}, CONTRACT)
    system = seen["system"] if isinstance(seen["system"], str) else json.dumps(seen["system"])
    assert "1-based" in system and "never a part" in system
    assert "value null" in system


def test_identifier_attributes_are_stated_from_the_subject():
    facts = [{"fact_type": "route", "subject": {"route_id": "R-X1"}, "attribute": "mode", "value": "road",
              "confidence": "high"}]
    env = _envelope({"facts": facts}, "master_table", {}, {"route": {"route_id", "mode"}})
    got = {(f["attribute"], f["value"]) for f in env["facts"]}
    assert got == {("mode", "road"), ("route_id", "R-X1")}
    again = _envelope({"facts": facts + [{**facts[0], "attribute": "route_id", "value": "R-X1"}]}, "master_table", {},
                      {"route": {"route_id", "mode"}})
    assert len(again["facts"]) == 2                     # not duplicated when the model already wrote it


def test_subject_keys_not_in_the_attributes_are_not_copied():
    facts = [{"fact_type": "oc_line", "subject": {"po_number": "P-X1", "po_line": 1}, "attribute": "qty", "value": 5}]
    env = _envelope({"facts": facts}, "order_confirmation", {}, {"oc_line": {"qty"}})
    assert [f["attribute"] for f in env["facts"]] == ["qty"]


def test_measured_value_with_unit_is_split():
    f = {**fact("1 250 kg", attribute="gross_weight"), "unit": None}
    env = _envelope({"facts": [f]}, "delivery_note_scan", {})
    assert (env["facts"][0]["value"], env["facts"][0]["unit"]) == (1250, "kg")
    plain = _envelope({"facts": [fact("12 pcs", attribute="note")]}, "delivery_note_scan", {})
    assert plain["facts"][0]["value"] == "12 pcs"          # only measured attributes are split


def test_slash_date_is_day_first_only_for_day_first_languages():
    f = fact("03/04/2031", attribute="note_date")
    assert _envelope({"facts": [f], "languages": ["de"]}, "x", {})["facts"][0]["value"] == "2031-04-03"
    assert _envelope({"facts": [f], "languages": ["en"]}, "x", {})["facts"][0]["value"] == "03/04/2031"
    assert _envelope({"facts": [f]}, "x", {})["facts"][0]["value"] == "03/04/2031"


def test_workbook_rows_reach_the_model_as_text(monkeypatch):
    import io
    from openpyxl import Workbook
    import kb.llm.base as base
    wb = Workbook(); ws = wb.active; ws.title = "Lines"
    ws.append(["part", "qty"]); ws.append(["P-X1", 5]); ws.append([None, None]); ws.append(["P-X2", 7])
    buf = io.BytesIO(); wb.save(buf)
    text, _ = base.text_of("xlsx", buf.getvalue())
    assert text.splitlines() == ["# sheet Lines", "part\tqty", "P-X1\t5", "P-X2\t7"]
    monkeypatch.setattr(base, "XLSX_MAX_ROWS", 2)
    assert base.text_of("xlsx", buf.getvalue())[0].splitlines()[-1] == "[1 more rows in this sheet not shown]"
