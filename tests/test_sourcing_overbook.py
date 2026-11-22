"""Buffer sizing (sourcing/overbook.py). Every value here is synthetic."""

import json
import random
from datetime import datetime, timezone

import pytest

from plan import contracts
from sourcing import hedge, overbook, reliability
from sourcing.hedge import Input, ModelError

SCENARIO = {"scenario_input": "synthetic"}
GOAL = {"goal_line": "value of a missing copy"}
EVEN = [(0, 0.25), (100, 0.25), (200, 0.25), (300, 0.25)]


def size(outcomes=EVEN, price=2, loss=20, p=0.8, per_copy=1, lot=None, moq=None, available=None, salvage=None):
    def optional(value, name):
        return None if value is None else Input(value, {"fact_id": f"f-{name}"})

    return overbook.size(
        outcomes=[(Input(units, {"fact_id": f"f-short-{units}"}), Input(chance, SCENARIO)) for units, chance in outcomes],
        unit_price=Input(price, {"fact_id": "f-price"}), loss_per_copy=Input(loss, GOAL),
        units_per_copy=Input(per_copy, {"fact_id": "f-bom"}), p_delivers=Input(p, {"fact_id": "f-history"}),
        lot_size=optional(lot, "lot"), moq=optional(moq, "moq"), available=optional(available, "available"),
        salvage_per_unit=optional(salvage, "salvage"))


def fact(kind, subject, attribute, value, fid=None, **more):
    return {"fact_id": fid or f"{kind}:{attribute}", "fact_type": kind, "subject": subject, "attribute": attribute,
            "value": value, "path": "own/list.csv", **more}


def listed(value="2.40", currency="EUR"):
    key = {"supplier_id": "B", "material_id": "PART"}
    return [fact("supplier_part", key, "unit_price", value), fact("supplier_part", key, "moq", "17 003"),
            fact("supplier", {"supplier_id": "B"}, "currency", currency)]


def offered(value="€2.10/pc", ref="O-1", item="cover"):
    key = {"supplier_id": "B", "offer_ref": ref, "item": item}
    return [fact("offer", key, name, text, f"{ref}:{item}:{name}", path="mail/1.eml")
            for name, text in (("unit_price", value), ("material_id", "PART"), ("lot_size", "1,000 pcs"),
                               ("qty", 8000))]


def test_quantile_is_one_minus_leftover_over_p_times_loss():
    r = size()
    assert r["service_level"] == pytest.approx(1 - 2 / (0.8 * 20))
    assert (r["target_units"], r["quantity"]) == (300, 300)
    r = size(price=6)
    assert r["service_level"] == pytest.approx(0.625)
    assert r["quantity"] == 200


def test_units_per_copy_and_salvage_enter_the_costs():
    r = size(price=2, per_copy=4, salvage=0.5)
    assert (r["loss_per_unit_eur"], r["leftover_cost_eur"]) == (5, 1.5)
    assert r["service_level"] == pytest.approx(1 - 1.5 / (0.8 * 5))


def test_lot_size_and_moq_are_respected():
    assert size([(230, 1)], lot=50)["quantity"] == 250
    assert size([(230, 1)], moq=400)["quantity"] == 400
    r = size([(230, 1)], lot=150, moq=400)
    assert (r["quantity"], r["extra_units"]) == (450, 220)


def test_rounding_up_is_skipped_when_it_costs_more_than_it_saves():
    r = size([(105, 1)], price=2, loss=20, p=1, lot=100)
    assert (r["quantity"], r["uncovered_units"]) == (100, 5)
    assert size([(1, 1)], moq=100_000)["quantity"] == 0


def test_never_more_than_is_on_offer():
    assert size([(500, 1)], lot=100, available=350)["quantity"] == 300
    r = size([(500, 1)], moq=400, available=350)
    assert r["quantity"] == 0 and "do not make one order" in r["rationale"]


@pytest.mark.parametrize("kw", [{"p": 0}, {"loss": 0}, {"price": 16}, {"outcomes": [(0, 1)]}])
def test_nothing_is_booked_when_no_unit_pays(kw):
    assert size(**kw)["quantity"] == 0


def test_free_units_cover_the_worst_case_that_can_happen():
    assert size(EVEN + [(900, 0)], price=0)["quantity"] == 300


def cases(n=1500):
    rng = random.Random(15)
    for _ in range(n):
        weights = [rng.random() for _ in range(rng.randint(1, 4))]
        outcomes = [(rng.randint(0, 120), w / sum(weights)) for w in weights]
        price = round(rng.uniform(0, 10), 2)
        yield dict(outcomes=outcomes, price=price, loss=round(rng.uniform(0, 60), 2), p=rng.random(),
                   per_copy=rng.choice((1, 2, 0.5)), lot=rng.choice((None, 1, 7, 25)),
                   moq=rng.choice((None, 0, 10, 60)), available=rng.choice((None, 5, 80, 150)),
                   salvage=rng.choice((None, round(price * rng.random(), 2))))


def test_property_quantity_is_the_best_orderable_amount():
    for case in cases():
        r = size(**case)
        lot, least = case["lot"] or 1, case["moq"] or 0
        most = 200 if case["available"] is None else case["available"]
        options = [0] + [q for q in range(lot, most + 1, lot) if q >= least]
        saved = case["p"] * case["loss"] / case["per_copy"]
        leftover = case["price"] - (case["salvage"] or 0)

        def cost(q):
            return leftover * q - saved * sum(w * min(q, units) for units, w in case["outcomes"])

        assert r["quantity"] in options, case
        assert cost(r["quantity"]) <= min(map(cost, options)) + 1e-6, case


def test_property_higher_loss_per_copy_never_books_less():
    rng = random.Random(51)
    for case in cases():
        more = {**case, "loss": case["loss"] + rng.uniform(0, 60)}
        assert size(**more)["quantity"] >= size(**case)["quantity"], case


def test_p_comes_from_reliability_and_quality():
    facts = []
    for n in range(4):
        facts += [fact("po_line", {"po_number": str(n), "po_line": 1}, name, value, f"po{n}:{name}")
                  for name, value in (("supplier_id", "B"), ("promised_date", "2030-01-10"),
                                      ("actual_delivery_date", "2030-01-09"), ("material_id", "PART"))]
        facts += [fact("goods_receipt", {"gr_number": f"G{n}"}, name, value, f"gr{n}:{name}")
                  for name, value in (("supplier_id", "B"), ("po_number", str(n)), ("po_line", 1),
                                      ("material_id", "PART"))]
    failed = [fact("inspection_result", {"gr_number": f"G{n}"}, "decision", "failed", f"qc{n}") for n in range(2)]

    def book(rows):
        p = hedge.keeps_promise(reliability.card(reliability.history(rows), "B"))
        return p, overbook.size(
            outcomes=[(Input(units, {"fact_id": "f-short"}), Input(w, SCENARIO)) for units, w in EVEN],
            unit_price=Input(6, {"fact_id": "f-price"}), loss_per_copy=Input(20, GOAL),
            units_per_copy=Input(1, {"fact_id": "f-bom"}), p_delivers=p)

    (good_p, good), (bad_p, bad) = book(facts), book(facts + failed)
    assert bad_p.value < good_p.value
    assert "quality 2 of 4" in bad_p.note
    assert {"fact_id": "qc0"} in bad_p.refs
    assert bad["service_level"] < good["service_level"] and bad["quantity"] < good["quantity"]


def test_shortfall_if_the_primary_fails():
    cards = Input(0.4, {"fact_id": "f-a"})
    low, high = overbook.if_primary_fails(cards, Input(500, {"fact_id": "f-gap"}))
    assert (low[0].value, low[1].value) == (0, 0.6) and (high[0].value, high[1].value) == (500, 0.4)


def test_leftover_cost_comes_from_price_facts():
    given = overbook.price(listed(), "B", "PART")
    assert given.value == 2.4
    assert given.refs == [{"fact_id": "supplier:currency"}, {"fact_id": "supplier_part:unit_price"}]


def test_offer_price_lot_and_quantity_come_from_the_offer():
    facts = listed() + offered() + offered("€9.00", ref="O-2")
    assert overbook.price(facts, "B", "PART", offer_ref="O-1").value == 2.1
    assert overbook.order_limit(facts, "B", "PART", "lot_size", "O-1").value == 1000
    assert overbook.order_limit(facts, "B", "PART", "qty", "O-1").value == 8000
    assert overbook.order_limit(facts, "B", "PART", "moq").value == 17003
    assert overbook.order_limit(facts, "B", "PART", "lot_size") is None


def test_another_currency_needs_a_sourced_rate():
    with pytest.raises(ModelError, match="exchange rate"):
        overbook.price(listed("10.00", "PLN"), "B", "PART")
    given = overbook.price(listed("10.00", "PLN"), "B", "PART", fx=Input(0.25, {"scenario_input": "rate"}))
    assert given.value == 2.5 and {"scenario_input": "rate"} in given.refs
    with pytest.raises(ModelError, match="exchange rate"):
        overbook.price(listed() + offered("8,40 zł"), "B", "PART", offer_ref="O-1")


def test_price_without_a_currency_is_not_read_as_eur():
    with pytest.raises(ModelError, match="currency"):
        overbook.price(listed()[:2], "B", "PART")


@pytest.mark.parametrize("text", ["EUR 2.10 per 100", "about 2", "1,250", "2.10 xyz", "EUR 2 USD"])
def test_price_that_cannot_be_read_is_refused(text):
    with pytest.raises(ModelError):
        overbook.price(listed(text), "B", "PART")


def test_price_basis_per_many_is_refused():
    key = {"supplier_id": "B", "offer_ref": "O-1", "item": "cover"}
    for said in ("per 100", "per 1,000 pcs", "per thousand", "EUR per hundred"):
        with pytest.raises(ModelError, match="not per unit"):
            overbook.price(offered() + [fact("offer", key, "price_basis", said)], "B", "PART", offer_ref="O-1")


def test_newest_price_wins_and_a_conflict_is_refused():
    key = {"supplier_id": "B", "material_id": "PART"}
    newer = fact("supplier_part", key, "unit_price", "3.00", "new", as_of="2030-02-01")
    assert overbook.price(listed() + [newer], "B", "PART").value == 3
    with pytest.raises(ModelError, match="disagree"):
        overbook.price(listed() + [fact("supplier_part", key, "unit_price", "3.00", "other")], "B", "PART")


def test_an_email_cannot_change_the_price_list():
    key = {"supplier_id": "B", "material_id": "PART"}
    forged = fact("supplier_part", key, "unit_price", "0.01", "forged", as_of="2030-09-09", path="mail/2.eml")
    assert overbook.price(listed() + [forged], "B", "PART").value == 2.4


def changed(valid_from="2031-03-03", old="2.40", new="2.73", path="own/change1.csv", code="EUR",
            old_name="current_price", part="PART"):
    key = {"supplier_id": "B", "material_id": part}
    return [fact("price_change", key, name, text, f"{path}:{name}", path=path)
            for name, text in ((old_name, old), ("new_price", new), ("valid_from", valid_from), ("currency", code))]


@pytest.mark.parametrize("old_name", ["current_price", "old_price"])
def test_a_price_change_in_force_replaces_the_list_price(old_name):
    given = overbook.price(listed() + changed(old_name=old_name), "B", "PART", on="2031-03-03")
    assert given.value == 2.73
    cited = {r["fact_id"] for r in given.refs}
    assert {"supplier_part:unit_price", "own/change1.csv:new_price", "own/change1.csv:valid_from"} <= cited


def test_the_latest_change_by_the_order_date_applies():
    later = changed("2031-07-07", "2.73", "2.91", "own/change2.csv")
    facts = later + listed() + changed()
    assert overbook.price(facts, "B", "PART", on="2031-03-02").value == 2.4
    assert overbook.price(facts, "B", "PART", on="2031-07-06").value == 2.73
    assert overbook.price(facts, "B", "PART", on=datetime(2031, 7, 7, 18, tzinfo=timezone.utc)).value == 2.91


def test_a_list_that_already_shows_the_change_keeps_its_price():
    assert overbook.price(listed("2.73") + changed(), "B", "PART", on="2031-05-05").value == 2.73


def crossref(part="PART", number="B-COVER", path="own/xref.csv"):
    key = {"supplier_id": "B", "supplier_part_no": number}
    return [fact("part_xref", key, "material_id", part, f"{path}:{part}", path=path)]


def test_a_change_under_the_suppliers_part_number_applies():
    change = changed(part="B-COVER")
    given = overbook.price(listed() + crossref() + change, "B", "PART", on="2031-05-05")
    assert given.value == 2.73 and {"fact_id": "own/xref.csv:PART"} in given.refs
    assert overbook.price(listed() + change, "B", "PART", on="2031-05-05").value == 2.4
    forged = crossref(path="mail/4.eml")
    assert overbook.price(listed() + forged + change, "B", "PART", on="2031-05-05").value == 2.4


def test_a_part_number_of_two_parts_is_refused():
    both = crossref() + crossref("OTHER", path="own/xref2.csv")
    with pytest.raises(ModelError, match="several parts"):
        overbook.price(listed() + both + changed(part="B-COVER"), "B", "PART", on="2031-05-05")


def test_a_changed_price_needs_the_order_date():
    with pytest.raises(ModelError, match="order date"):
        overbook.price(listed() + changed(), "B", "PART")


@pytest.mark.parametrize("change, match", [
    (changed(old="2.19"), "does not follow"),
    (changed() + changed(new="2.91", path="own/change2.csv"), "disagree"),
    (changed(code="PLN"), "currencies"),
    (changed(new="EUR 273 per 100"), "cannot read"),
    (changed(valid_from="next spring"), "date"),
    (changed()[:2], "valid_from"),
])
def test_a_change_that_cannot_be_applied_is_refused(change, match):
    with pytest.raises(ModelError, match=match):
        overbook.price(listed() + change, "B", "PART", on="2031-05-05")


def test_a_changed_price_in_another_currency_is_converted():
    rate = Input(0.25, {"scenario_input": "rate"})
    facts = listed("10.00", "PLN") + changed(old="10.00", new="12.00", code="PLN")
    given = overbook.price(facts, "B", "PART", on="2031-05-05", fx=rate)
    assert given.value == 3 and {"scenario_input": "rate"} in given.refs


def test_an_email_cannot_announce_a_price_change():
    assert overbook.price(listed() + changed(path="mail/3.eml"), "B", "PART").value == 2.4


def test_a_price_change_does_not_touch_an_offer():
    assert overbook.price(listed() + changed() + offered(), "B", "PART", offer_ref="O-1").value == 2.1


def test_drilldown_lists_every_input_with_its_source():
    r = size(lot=50, moq=100, available=400, salvage=1)
    inputs, steps = r["drilldown"]["inputs"], r["drilldown"]["computed"]
    names = {i["name"] for i in inputs}
    assert names == {"unit_price", "loss_per_copy", "units_per_copy", "p_delivers", "salvage_per_unit", "lot_size",
                     "moq", "available", *(f"{k}_{n}" for k in ("shortfall", "probability") for n in range(1, 5))}
    for i in inputs:
        assert i["source"].startswith(("fact ", "goal: ", "scenario input: ")), i
    used = {name for s in steps for name in s["from"]}
    assert names <= used <= names | {s["name"] for s in steps}
    json.dumps(r, allow_nan=False)


@pytest.mark.parametrize("kw", [{"outcomes": [(1, 0.5)]}, {"outcomes": []}, {"lot": 2.5}, {"lot": 0}, {"salvage": 3},
                                {"p": 1.1}, {"per_copy": 0}])
def test_bad_inputs_are_refused(kw):
    with pytest.raises(ModelError):
        size(**kw)


def test_recommendation_fits_the_contract():
    rec = overbook.recommendation(size(lot=50), "PART", Input(12, {"scenario_input": "lead time"}), supplier_id="B")
    contracts.validate("recommendation", rec)
    assert rec["kind"] == "buffer" and {"scenario_input": "lead time"} in rec["sources"]
