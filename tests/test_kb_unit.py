"""Unit tests for kb: tabular parsing and the rule engine. Synthetic data only."""

from datetime import datetime, timezone

from kb.engine import Fact, build_records, evaluate
from kb.rules import RuleError, validate_rule
from kb.tabular import find_header, parse_value, read_tables, signature, table_to_facts

import pytest

NOW = datetime(2030, 1, 15, tzinfo=timezone.utc)


def test_signature_ignores_order_case_and_empty_cells():
    assert signature(["B", "a", ""]) == signature([" A", "b"])
    assert signature(["a"], "Sheet1") != signature(["a"])


def test_semicolon_csv_with_decimal_comma_and_us_dates():
    data = "ORDNO;ITEM;VENDOR;PRICE;DATE\r\n9001;00010;7;1,2500;03/04/2030\r\n9002;00020;12;2,0000;12/31/2030\r\n"
    tables = read_tables("csv", data.encode())
    mapping = {"fact_type": "order_line", "subject": ["order", "line"], "as_of": {"field": "date"}, "columns": {
        "ORDNO": {"field": "order", "type": "text"}, "ITEM": {"field": "line", "type": "int"},
        "VENDOR": {"field": "vendor", "type": "int", "id_template": "V-{:03d}"},
        "PRICE": {"field": "price", "type": "decimal", "decimal_sep": ","},
        "DATE": {"field": "date", "type": "date", "date_format": "%m/%d/%Y"}}}
    maps = {signature(["ORDNO", "ITEM", "VENDOR", "PRICE", "DATE"]): mapping}
    idx, found = find_header(tables[0], maps, "csv")
    facts, stats = table_to_facts(tables[0], idx, found, path="x.csv", sha256="s")
    by = {(f["subject"]["order"], f["attribute"]): f for f in facts}
    assert stats["rows"] == 2 and stats["parse_errors"] == 0
    assert by[("9001", "price")]["value"] == 1.25
    assert by[("9001", "vendor")]["value"] == "V-007"
    assert by[("9002", "date")]["value"] == "2030-12-31"
    assert by[("9002", "date")]["as_of"] == "2030-12-31"
    assert by[("9001", "price")]["evidence"] == "1,2500"
    assert by[("9001", "price")]["subject"] == {"order": "9001", "line": 10}


def test_renamed_column_means_no_map():
    tables = read_tables("csv", b"a,b_renamed\n1,2\n")
    assert find_header(tables[0], {signature(["a", "b"]): {"fact_type": "t", "columns": {}}}, "csv") is None


def test_parse_value_thousands_and_enum_and_errors():
    assert parse_value("640.000", {"type": "int", "thousands_sep": "."}) == (640000, None)
    assert parse_value("8 000", {"type": "int"}) == (8000, None)
    assert parse_value("GL", {"type": "text", "enum": {"gl": "done"}}) == ("done", None)
    value, err = parse_value("abc", {"type": "int"})
    assert value is None and err


def _fact(fid, ftype, subject, attr, value, *, conf=1.0, as_of=None, extractor="x", path="p", sha="s"):
    return Fact(fid, ftype, subject, attr, value, as_of, conf, extractor, path, sha)


RULE = {"rule_id": "qty", "question": "How many?", "key": ["note"], "compare": "number", "decide": "preference",
        "min_confidence": 0.6, "fallback": "lowest", "severity": "high",
        "explanation": "We believe the {winner} ({winner_value}); {others}.",
        "candidates": [{"label": "paper", "fact_type": "note_line", "attribute": "final_qty"},
                       {"label": "erp", "fact_type": "receipt", "attribute": "qty", "key_map": {"note": "note_no"}}]}


def test_contradiction_and_preference():
    facts = [_fact("f1", "note_line", {"note": "N1"}, "final_qty", 10, sha="a"),
             _fact("f2", "receipt", {"rid": "R1"}, "qty", 100, sha="b"),
             _fact("f3", "receipt", {"rid": "R1"}, "note_no", "N1", sha="b")]
    believed, issues, _ = evaluate([RULE], build_records(facts), {}, NOW)
    assert len(believed) == 1 and believed[0].value == 10 and believed[0].winner_label == "paper"
    assert believed[0].contradiction
    assert issues[0].severity == "high" and "paper 10 vs erp 100" in issues[0].title
    assert set(issues[0].fact_ids) == {"f1", "f2"}


def test_agreement_is_no_issue_and_low_confidence_falls_back_to_lowest():
    agree = [_fact("f1", "note_line", {"note": "N1"}, "final_qty", 5, sha="a"),
             _fact("f2", "receipt", {"note_no": "N1"}, "qty", "5", sha="b")]
    believed, issues, _ = evaluate([RULE], build_records(agree), {}, NOW)
    assert believed[0].value == 5 and not issues
    unsure = [_fact("f1", "note_line", {"note": "N1"}, "final_qty", 50, conf=0.4, sha="a"),
              _fact("f2", "receipt", {"note_no": "N1"}, "qty", 30, conf=0.4, sha="b")]
    believed, _, _ = evaluate([RULE], build_records(unsure), {}, NOW)
    assert believed[0].value == 30


def test_decide_none_needs_a_person_and_value_map_drops_records():
    rule = {"rule_id": "r", "key": ["k"], "compare": "text", "decide": "none",
            "candidates": [{"fact_type": "a", "attribute": "v"},
                           {"fact_type": "b", "attribute": "v", "value_map": {"skip": None}}]}
    facts = [_fact("1", "a", {"k": 1}, "v", "X", sha="1"), _fact("2", "b", {"k": 1}, "v", "Y", sha="2"),
             _fact("3", "a", {"k": 2}, "v", "X", sha="1"), _fact("4", "b", {"k": 2}, "v", "skip", sha="2")]
    believed, issues, _ = evaluate([rule], build_records(facts), {}, NOW)
    by_key = {b.key["k"]: b for b in believed}
    assert by_key["1"].value is None and issues[0].payload["needs_decision"]
    assert by_key["2"].value == "X" and not by_key["2"].contradiction


def test_change_record_derives_a_believed_value_once_effective():
    rule = {"rule_id": "rev", "key": ["item"], "compare": "text", "decide": "preference",
            "candidates": [{"label": "change", "fact_type": "item", "attribute": "rev", "extractor": "derived:*"},
                           {"label": "master", "fact_type": "item", "attribute": "rev", "extractor_not": "derived:*"}]}
    base = [_fact("m", "item", {"item": "I1"}, "rev", "B", sha="m")]
    change = [_fact(f"c{i}", "change_record", {"item": "I1"}, a, v, sha="c")
              for i, (a, v) in enumerate([("field", "rev"), ("new", "C"), ("effective_from", "2030-01-10")])]
    believed, issues, diag = evaluate([rule], build_records(base + change), {}, NOW)
    assert believed[0].value == "C" and believed[0].winner_label == "change" and issues
    future = [_fact(f"c{i}", "change_record", {"item": "I1"}, a, v, sha="c")
              for i, (a, v) in enumerate([("field", "rev"), ("new", "C"), ("effective_from", "2030-02-01")])]
    believed, issues, diag = evaluate([rule], build_records(base + future), {}, NOW)
    assert believed[0].value == "B" and not issues and diag["derived_skipped"]


def test_sum_aggregate_and_tolerance():
    rule = {"rule_id": "stock", "key": ["m"], "compare": "number", "tolerance_pct": 1, "decide": "preference",
            "candidates": [{"fact_type": "batch", "attribute": "qty", "aggregate": "sum"},
                           {"fact_type": "plan", "attribute": "stock"}]}
    facts = [_fact("1", "batch", {"m": "A", "b": 1}, "qty", 60, sha="1"),
             _fact("2", "batch", {"m": "A", "b": 2}, "qty", 40, sha="1"),
             _fact("3", "plan", {"m": "A"}, "stock", 100.5, sha="2")]
    believed, issues, _ = evaluate([rule], build_records(facts), {}, NOW)
    assert believed[0].value == 100 and not issues


def test_rule_validation():
    with pytest.raises(RuleError):
        validate_rule({"rule_id": "x", "key": [], "candidates": [{"fact_type": "a", "attribute": "b"}]})
    with pytest.raises(RuleError):
        validate_rule({"rule_id": "x", "key": ["k"], "candidates": [{"fact_type": "a"}]})
    validate_rule(RULE)


def test_change_record_with_field_in_subject():
    rule = {"rule_id": "rev", "key": ["item"], "compare": "text", "decide": "preference",
            "candidates": [{"label": "change", "fact_type": "item", "attribute": "rev", "extractor": "derived:*"},
                           {"label": "master", "fact_type": "item", "attribute": "rev", "extractor_not": "derived:*"}]}
    base = [_fact("m", "item", {"item": "I1"}, "rev", "B", sha="m")]
    change = [_fact(f"c{i}", "change_record", {"item": "I1", "field": "rev"}, a, v, sha="c")
              for i, (a, v) in enumerate([("old", "B"), ("new", "C")])]
    believed, issues, _ = evaluate([rule], build_records(base + change), {}, NOW)
    assert believed[0].value == "C" and believed[0].key == {"item": "I1"}


def test_resolver_line_match_and_text_parts():
    from kb.resolve import resolve
    po = [_fact(f"p{i}{a}", "po_line", {"po": "P", "line": i}, a, v, sha="po")
          for i, m, q in [(1, "M2", 13), (3, "M1", 84), (5, "M1", 240)] for a, v in [("material", m), ("qty", q)]]
    oc = [_fact(f"o{a}", "oc_line", {"po": "P", "line": 1}, a, v, sha="oc1")
          for a, v in [("material", "M1"), ("qty", 84)]]
    xref = [_fact("x", "xref", {"part": "SP-9"}, "material", "M7", sha="c")]
    stmt = [_fact("s1", "stmt", {"po": "P", "no": 1, "material": "M3"}, "value", "Artwork SP-9: rev. B", sha="oc2")]
    records = build_records(po + oc + xref + stmt)
    changes = resolve(records, {
        "line_match": [{"source": "oc_line", "target": "po_line", "document_key": "po", "line_key": "line",
                        "match": ["material", "qty"]}],
        "text_parts": [{"fact_types": ["stmt"], "text_attributes": ["value"], "material_key": "material",
                        "xref": {"fact_type": "xref", "part_attribute": "part", "material_attribute": "material"}}]})
    moved = {c["fact_type"]: c["to"] for c in changes}
    assert moved == {"oc_line": 3, "stmt": "M7"}
    assert next(r for r in records if r.fact_type == "oc_line").subject["line"] == 3
