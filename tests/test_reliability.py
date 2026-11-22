"""Supplier reliability on synthetic facts."""

import inspect
import json
from datetime import date

import pytest

from kb import profiles
from kb.engine import Fact, build_records
from plan import contracts
from sourcing import reliability as rel
from tracker import lookahead


def rows(fact_type, subject, doc="doc", as_of=None, **attrs):
    tag = "-".join(str(v) for v in subject.values())
    return [{"fact_id": f"{doc}:{fact_type}:{tag}:{name}", "fact_type": fact_type, "subject": subject,
             "attribute": name, "value": value, "as_of": as_of, "path": doc, "sha256": doc, "extractor": "test"}
            for name, value in attrs.items()]


def line(po, number=1, sup="S1", promised="2030-01-10", actual="2030-01-10", qty=100, part="P1", **kw):
    return rows("po_line", {"po_number": po, "po_line": number}, supplier_id=sup, promised_date=promised,
                actual_delivery_date=actual, qty=qty, material_id=part, **kw)


def receipt(gr, po, number=1, sup="S1", received=100, ordered=100, part="P1", note=None):
    return rows("goods_receipt", {"gr_number": gr}, gr_number=gr, supplier_id=sup, po_number=po, po_line=number,
                qty_received=received, qty_ordered=ordered, material_id=part, delivery_note=note or f"N-{gr}")


def inspection(gr, decision):
    return rows("inspection_result", {"gr_number": gr}, decision=decision)


def orders(sup, kept, broken):
    return [f for i in range(kept + broken)
            for f in line(f"{sup}-{i}", sup=sup, actual="2030-01-10" if i < kept else "2030-01-12")]


def measure(record, name):
    return next(m for m in record["score"]["measures"] if m["measure"] == name)


def card(facts, sup="S1", believed=(), **kw):
    return rel.card(rel.history(facts, believed), sup, **kw)


def test_value_is_the_beta_posterior_mean():
    score = card(orders("S1", 6, 2) + orders("S2", 2, 6))["score"]
    assert (score["prior"], score["n"], score["k"]) == (0.5, 8, 5)
    assert score["posterior"]["alpha"] == 8.5 and score["posterior"]["beta"] == 4.5
    assert score["value"] == pytest.approx(8.5 / 13)


def test_thin_history_is_pulled_toward_the_average():
    h = rel.history(orders("THIN", 1, 0) + orders("THICK", 40, 0) + orders("BAD", 0, 41))
    thin, thick = (rel.card(h, s)["score"] for s in ("THIN", "THICK"))
    assert thin["prior"] == 0.5
    assert abs(thin["value"] - 0.5) < 0.1 < 0.4 < abs(thick["value"] - 0.5)


def test_on_time_is_promised_against_actual_and_matches_kb_profiles():
    facts = (line("A") + line("B", actual="2030-01-11") + line("C", actual="2030-01-09")
             + line("D", actual=None) + line("E", promised=None))
    row = measure(card(facts), "on_time")
    assert (row["kept"], row["n"]) == (2, 3)

    records = build_records([Fact(f["fact_id"], f["fact_type"], f["subject"], f["attribute"], f["value"], None, 1.0,
                                  "test", "doc", "doc") for f in facts])
    delivery = profiles.build(records, [], date(2030, 2, 1))["S1"]["delivery"]
    assert (row["n"], row["n"] - row["kept"]) == (delivery["delivered_lines"], delivery["late_lines"])


def test_partial_receipts_add_up():
    facts = line("A") + receipt("G1", "A", received=60) + receipt("G2", "A", received=40)
    assert (measure(card(facts), "in_full")["kept"], card(facts)["score"]["inputs"][0]["received"]) == (1, 100)


def test_short_delivery_breaks_the_promise():
    score = card(line("A") + receipt("G1", "A", received=90))["score"]
    assert score["inputs"][0]["on_time"] and not score["inputs"][0]["in_full"]
    assert score["history"] == {"kept": 0, "n": 1}


def test_over_delivery_is_in_full():
    assert measure(card(line("A") + receipt("G1", "A", received=110)), "in_full")["kept"] == 1


def test_believed_quantity_corrects_the_booking():
    believed = [{"rule_id": "received_qty", "key": {"delivery_note": "N-G1", "material_id": "P1"}, "value": 80,
                 "winning_fact_ids": ["scan-fact"]}]
    record = card(line("A") + receipt("G1", "A"), believed=believed)
    assert record["score"]["inputs"][0]["received"] == 80
    assert "scan-fact" in measure(record, "in_full")["fact_ids"]


def test_believed_quantity_of_a_shared_note_is_not_counted_twice():
    believed = [{"rule_id": "received_qty", "key": {"delivery_note": "N", "material_id": "P1"}, "value": 100}]
    facts = line("A", qty=200) + receipt("G1", "A", received=50, note="N") + receipt("G2", "A", received=50, note="N")
    assert card(facts, believed=believed)["score"]["inputs"][0]["received"] == 100


def test_receipt_with_other_line_numbering_finds_its_line():
    facts = line("A", 10) + line("A", 20, part="P2") + receipt("G1", "A", 2, part="P2", received=70)
    first, second = card(facts)["score"]["inputs"]
    assert "received" not in first
    assert second["received"] == 70 and second["in_full"] is False


def test_same_line_number_with_another_material_is_not_a_match():
    facts = line("A", 1, part="P1") + line("A", 2, part="P2") + receipt("G1", "A", 1, part="P2")
    first, second = card(facts)["score"]["inputs"]
    assert "received" not in first and second["received"] == 100


def test_ordered_quantity_separates_lines_of_one_material():
    facts = line("A", 10, qty=100) + line("A", 20, qty=300) + receipt("G1", "A", 2, received=300, ordered=300)
    assert [p.get("received") for p in card(facts)["score"]["inputs"]] == [None, 300]


def test_ambiguous_receipt_is_reported_and_never_makes_a_line_short():
    facts = line("A", 10) + line("A", 20) + receipt("G1", "A", 10, received=60) + receipt("G2", "A", 3, received=40)
    record = card(facts)
    assert measure(record, "in_full")["n"] == 0
    assert record["score"]["not_judged"]["receipts without a certain PO line"] == 1


@pytest.mark.parametrize("line_part, receipt_part", [("P1", None), (None, "P1"), (None, None)])
def test_unplaced_receipt_without_a_material_never_makes_a_line_short(line_part, receipt_part):
    facts = (line("A", 10, part=line_part) + receipt("G1", "A", 10, received=60)
             + receipt("G2", "A", 3, received=40, part=receipt_part))
    record = card(facts)
    assert measure(record, "in_full")["n"] == 0
    assert record["score"]["history"] == {"kept": 1, "n": 1}


def test_unplaced_receipt_of_another_material_does_not_hide_a_short_line():
    facts = line("A", 10) + receipt("G1", "A", 10, received=60) + receipt("G2", "A", 3, received=40, part="P2")
    assert card(facts)["score"]["inputs"][0]["in_full"] is False


def test_quantities_on_a_line_not_judged_in_full_cite_their_facts():
    facts = (line("A", 10) + receipt("G1", "A", 10, received=60)
             + receipt("G2", "A", 3, received=40, part=None))
    score = card(facts)["score"]
    entry = score["inputs"][0]
    cited = {"doc:po_line:A-10:qty", "doc:goods_receipt:G1:qty_received"}
    assert "in_full" not in entry and (entry["ordered"], entry["received"]) == (100, 60)
    assert cited <= set(entry["fact_ids"])
    assert cited <= {s.get("fact_id") for s in score["sources"]}


def test_ordered_quantity_without_a_receipt_cites_its_fact():
    entry = card(line("A"))["score"]["inputs"][0]
    assert entry["ordered"] == 100 and "doc:po_line:A-1:qty" in entry["fact_ids"]


def test_failed_inspection_breaks_an_on_time_promise():
    record = card(line("A") + receipt("G1", "A") + inspection("G1", "Rejected"))
    assert record["score"]["history"] == {"kept": 0, "n": 1}
    assert measure(record, "quality")["inspections"] == {"failed": 1}


def test_failed_inspection_counts_before_the_line_closes():
    record = card(line("A", actual=None) + receipt("G1", "A", received=10) + inspection("G1", "fail"))
    assert record["score"]["history"] == {"kept": 0, "n": 1}
    assert measure(record, "in_full")["n"] == 0


def test_failed_inspection_without_a_po_line_still_counts():
    record = card(receipt("G1", None) + inspection("G1", "failed"))
    assert record["score"]["history"] == {"kept": 0, "n": 1}


@pytest.mark.parametrize("decision, verdict", [("passed", "passed"), ("under discussion", "unclear")])
def test_only_a_failed_inspection_lowers_quality(decision, verdict):
    row = measure(card(line("A") + receipt("G1", "A") + inspection("G1", decision)), "quality")
    assert (row["kept"], row["inspections"]) == (1, {verdict: 1})


@pytest.mark.parametrize("first, second, verdict", [
    ("passed", "passed", "passed"), ("failed", "failed", "failed"),
    ("passed", "failed", "failed"), ("failed", "passed", "failed"),
    ("under discussion", "passed", "passed"), ("passed", "under discussion", "passed"),
])
def test_one_report_in_two_documents_counts_once(first, second, verdict):
    again = [dict(f, fact_id="b" + f["fact_id"], sha256="b", path="b") for f in inspection("G1", second)]
    row = measure(card(line("A") + receipt("G1", "A") + inspection("G1", first) + again), "quality")
    assert row["inspections"] == {verdict: 1}
    assert row["value"].endswith("(1 inspection reports)")


def test_reports_on_two_receipts_count_twice():
    facts = (line("A") + receipt("G1", "A", received=50) + receipt("G2", "A", received=50)
             + inspection("G1", "passed") + inspection("G2", "passed"))
    assert measure(card(facts), "quality")["inspections"] == {"passed": 2}


def test_each_measure_is_shrunk_toward_its_own_average():
    facts = (line("A") + receipt("G1", "A") + line("B", sup="S2") + receipt("G2", "B", sup="S2")
             + inspection("G2", "failed"))
    assert measure(card(facts), "quality")["posterior"]["mean"] == pytest.approx((0.5 * 5 + 1) / 6)
    assert measure(card(facts), "on_time")["posterior"]["mean"] == 1


def test_returns_are_not_promises():
    score = card(line("A", qty=-100))["score"]
    assert score["n"] == 0 and score["not_judged"] == {"returns and cancelled lines": 1}


def test_open_line_is_not_judged():
    assert card(line("A", actual=None))["score"]["n"] == 0


def test_the_same_line_in_two_exports_counts_once_and_the_newest_wins():
    old = line("A", actual="2030-01-12", doc="old", as_of="2030-01-12")
    new = line("A", doc="new", as_of="2030-01-13")
    assert card(old + new)["score"]["history"] == {"kept": 1, "n": 1}


def test_input_order_does_not_matter():
    facts = orders("S1", 3, 2) + receipt("G1", "S1-0") + inspection("G1", "failed")
    assert card(facts) == card(facts[::-1])


def test_every_input_and_count_is_in_the_payload():
    facts = line("A") + receipt("G1", "A", received=90) + inspection("G1", "failed")
    score = card(facts)["score"]
    assert score["inputs"][0] == {**score["inputs"][0], "promised": "2030-01-10", "actual": "2030-01-10",
                                  "ordered": 100, "received": 90}
    cited = {s.get("fact_id") for s in score["sources"]}
    wanted = {f["fact_id"] for f in facts if f["attribute"] in
              ("promised_date", "actual_delivery_date", "qty", "qty_received", "decision")}
    assert wanted <= cited
    for row in score["measures"]:
        assert {"kept", "n", "all_suppliers", "posterior"} <= set(row) and row["fact_ids"]
    assert score["starting_point"]["by_supplier"] == {"S1": {"kept": 0, "n": 1}}


def test_supplier_without_history_gets_the_average():
    score = card(orders("S1", 3, 1), sup="NEW")["score"]
    assert (score["value"], score["n"], score["starting_point"]["kind"]) == (0.75, 0, "all_suppliers")


def test_scenario_supplier_gets_a_labelled_prior():
    prior = {"value": 0.6, "label": "new supplier prior", "k": 4}
    score = card(orders("S1", 3, 1), sup="NEW", scenario_prior=prior)["score"]
    assert (score["value"], score["k"]) == (pytest.approx(0.6), 4)
    assert {"scenario_input": "new supplier prior", "value": 0.6} in score["sources"]
    assert "scenario input: new supplier prior" in score["method"]


def test_real_history_beats_a_scenario_prior():
    facts = orders("S1", 1, 3)
    with_prior = card(facts, scenario_prior={"value": 0.99, "label": "dropped score"})["score"]
    assert with_prior["value"] == card(facts)["score"]["value"]
    assert with_prior["scenario_prior_ignored"]["value"] == 0.99


def test_a_dropped_score_is_not_read():
    facts = orders("S1", 1, 3)
    drop = rows("supplier", {"supplier_id": "S1"}, name="Synthetic", score=0.99, reliability=0.99)
    assert card(facts + drop)["score"]["value"] == card(facts)["score"]["value"]
    assert list(inspect.signature(rel.card).parameters) == ["h", "supplier_id", "promises", "scenario_prior", "k"]


@pytest.mark.parametrize("prior", [{"value": 0.6}, {"value": 1.2, "label": "x"}, {"label": "x"},
                                   {"value": float("nan"), "label": "x"}, {"value": 0.6, "label": "x", "k": 0}])
def test_bad_scenario_prior_is_refused(prior):
    with pytest.raises(ValueError):
        card([], scenario_prior=prior)


def test_unknown_is_shown_as_unknown():
    record = card([])
    assert "value" not in record["score"] and record["score"]["method"].startswith("Unknown")
    contracts.validate_record(record)


def test_update_moves_the_score_both_ways():
    before = card(orders("S1", 3, 3))
    start = before["score"]["value"]
    assert rel.update(before, "kept", "PR-A")["score"]["value"] > start
    assert rel.update(before, "broken", "PR-A")["score"]["value"] < start
    assert before["score"]["value"] == start and before["score"]["outcomes"] == []


def test_update_lowers_a_scenario_prior():
    before = card([], scenario_prior={"value": 0.6, "label": "persona"})
    after = rel.update(before, "broken", "PR-A", at="2030-01-10T06:00:00+00:00")
    assert after["score"]["value"] == pytest.approx(0.5)
    assert after["cause"] == "broken promise PR-A"


def test_one_promise_counts_once():
    once = rel.update(card(orders("S1", 3, 3)), "kept", "PR-A")
    assert rel.update(once, "kept", "PR-A") == once
    flipped = rel.update(once, "broken", "PR-A")
    assert flipped["score"]["n"] == once["score"]["n"] and flipped["score"]["value"] < once["score"]["value"]


def test_update_refuses_other_outcomes():
    with pytest.raises(ValueError):
        rel.update(card([]), "cancelled", "PR-A")


def test_ledger_outcomes_match_updates():
    facts = orders("S1", 3, 3)
    ledger = [{"promise_id": pid, "supplier_id": sup, "state": state, "sources": [{"fact_id": f"mail-{pid}"}]}
              for pid, sup, state in (("A", "S1", "waiting"), ("A", "S1", "broken"), ("B", "S1", "proven"),
                                      ("C", "S1", "cancelled"), ("D", "S1", "waiting"), ("E", "S2", "broken"))]
    stepped = rel.update(rel.update(card(facts), "broken", "A"), "kept", "B")
    score = card(facts, promises=ledger)["score"]
    assert (score["value"], score["n"]) == (stepped["score"]["value"], 8)
    assert {"fact_id": "mail-A"} in score["sources"]


def test_cards_cover_history_priors_and_ledger():
    found = rel.cards(rel.history(orders("S1", 1, 1)),
                      [{"promise_id": "A", "supplier_id": "S3", "state": "broken"}],
                      {"S2": {"value": 0.6, "label": "persona"}})
    assert [c["supplier_id"] for c in found] == ["S1", "S2", "S3"]


def test_records_validate_fold_and_show_the_drop():
    facts = orders("S1", 4, 1) + rows("supplier", {"supplier_id": "S1"}, name="Synthetic")
    before = card(facts)
    after = rel.update(before, "broken", "PR-A", at="2030-01-10T06:00:00+00:00")
    for record in (before, after):
        contracts.validate_record(json.loads(json.dumps(record)))
        for row in record["score"]["breakdown"]:
            assert all(isinstance(row[key], str) and row[key] for key in ("label", "value", "source"))
    assert before["name"] == "Synthetic"
    state = json.dumps(lookahead.fold([{**before, "id": 1}, {**after, "id": 2}]), default=str)
    assert str(after["score"]["value"]) in state and "broken promise PR-A" in state
