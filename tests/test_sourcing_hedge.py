"""Hedge rule (sourcing/hedge.py). Every value here is synthetic."""

import json
import random

import pytest

from plan import contracts
from sourcing import hedge, reliability
from sourcing.hedge import Input, ModelError

FACT = {"fact_id": "f-1"}
GOAL = {"goal_line": "a missing copy costs 50 EUR"}
SCENARIO = {"scenario_input": "backup terms"}
MARGIN = {"statement": "All copies on time. Protect margin."}


def decide(p=0.3, copies=100, loss=50, units=120, price=10, per_copy=1, model="deposit_only", deposit=240,
           salvage=None, goal=None):
    return hedge.decide(
        p_fail=Input(p, FACT), copies_at_risk=Input(copies, {"fact_id": "f-2"}), loss_per_copy=Input(loss, GOAL),
        units=Input(units, SCENARIO), unit_price=Input(price, {"fact_id": "f-price"}),
        units_per_copy=Input(per_copy, {"fact_id": "f-bom"}), cost_model=Input(model, SCENARIO),
        deposit=None if deposit is None else Input(deposit, SCENARIO),
        salvage=None if salvage is None else Input(salvage, {"scenario_input": "salvage"}), goal=goal)


def computed(result, name):
    return next(r for r in result["drilldown"]["computed"] if r["name"] == name)


def test_deposit_only_commits_the_deposit():
    r = decide()
    assert (r["expected_loss_eur"], r["commitment_eur"], r["hedge_cost_eur"]) == (1500, 240, 240)
    assert r["verdict"] == "hedge"
    assert r["cost_model"] == {"kind": "fixed", "fixed_eur": 240, "basis": "deposit_only"}


def test_full_order_commits_the_order_minus_salvage():
    r = decide(p=0.2, model="full_order", deposit=None, salvage=87)
    assert (r["order_value_eur"], r["commitment_eur"], r["expected_loss_eur"]) == (1200, 1113, 1000)
    assert r["verdict"] == "hold"
    assert r["cost_model"]["basis"] == "full_order"
    assert decide(p=0.2, model="full_order", deposit=None, salvage=300)["verdict"] == "hedge"
    assert decide(model="full_order", deposit=None)["commitment_eur"] == 1200


def test_the_same_offer_flips_with_the_cost_model():
    assert decide(p=0.1)["verdict"] == "hedge"
    assert decide(p=0.1, model="full_order", deposit=None)["verdict"] == "hold"


@pytest.mark.parametrize("kw", [{"model": "guess"}, {"deposit": None}, {"salvage": 10}, {"deposit": 1201},
                                {"model": "full_order", "salvage": 1201}])
def test_an_unclear_cost_model_is_refused(kw):
    with pytest.raises(ModelError):
        decide(**kw)


def test_breakeven_is_commitment_over_copies_times_loss():
    even = decide()["breakeven_p_fail"]
    assert even == pytest.approx(240 / (100 * 50))
    assert decide(p=even + 1e-6)["hedge"] and not decide(p=even - 1e-6)["hedge"]


def test_a_tie_does_not_hedge():
    assert not decide(p=1 - 0.7, copies=100, loss=10, deposit=300)["hedge"]      # 1 - 0.7 is not exactly 0.3


@pytest.mark.parametrize("kw", [{"copies": 0}, {"loss": 0}, {"deposit": 1200, "loss": 11}])
def test_no_probability_can_justify_it(kw):
    r = decide(p=1, **kw)
    assert r["breakeven_p_fail"] is None and not r["hedge"]
    assert "No chance of failure" in r["rationale"]


def test_only_copies_the_backup_covers_count():
    r = decide(units=30, per_copy=2, deposit=0)
    assert (r["copies_at_risk"], r["copies_covered"]) == (100, 15)
    assert r["expected_loss_eur"] == 225


def test_protect_margin_hedges_less():
    plain, margin = decide(p=0.1, loss=26), decide(p=0.1, loss=26, goal=MARGIN)
    assert plain["hedge"] and not margin["hedge"]
    assert margin["hedge_cost_eur"] == pytest.approx(0.9 * 240 + 0.1 * 1200)
    assert margin["breakeven_p_fail"] > plain["breakeven_p_fail"]
    assert {"goal_line": "Protect margin"} in margin["sources"]


@pytest.mark.parametrize("goal", [
    MARGIN, {"statement": "protect our margins"}, {"protect": ["margin"]}, {"answers": {"sacrifice": "Protect margin"}},
    {"answers": [{"id": "limits", "a": "On time, but preserve margin"}]},
])
def test_goal_that_protects_margin(goal):
    assert hedge.margin_line(goal)


@pytest.mark.parametrize("goal", [
    None, {}, {"statement": "All on time. Goodwill can suffer. Don't bankrupt me."},
    {"statement": "Don't protect margin"}, {"statement": "No need to protect margin"},
    {"statement": "Protect margin", "sacrifices": ["margin"]},
    {"answers": [{"q": "Should we protect margin?", "a": "no"}]},
])
def test_goal_that_does_not_protect_margin(goal):
    assert hedge.margin_line(goal) is None


def cases(n=4000):
    rng = random.Random(15)
    for _ in range(n):
        units, price = rng.randint(1, 5000), round(rng.uniform(0, 40), 2)
        order = round(units * price, 2)
        full = rng.random() < 0.5
        yield dict(p=rng.random(), copies=rng.randint(0, 5000), loss=round(rng.uniform(0, 300), 2), units=units,
                   price=price, per_copy=rng.choice((1, 1, 2, 4, 0.5)),
                   model="full_order" if full else "deposit_only",
                   deposit=None if full else round(order * rng.random(), 2),
                   salvage=round(order * rng.random(), 2) if full else None,
                   goal=rng.choice((None, MARGIN)))


def test_property_higher_loss_per_copy_never_hedges_less():
    rng = random.Random(51)
    for case in cases():
        more = {**case, "loss": case["loss"] + rng.uniform(0, 300)}
        assert decide(**more)["hedge"] >= decide(**case)["hedge"], case


def test_property_protect_margin_never_hedges_more():
    flipped = 0
    for case in cases():
        plain, margin = decide(**{**case, "goal": None}), decide(**{**case, "goal": MARGIN})
        assert margin["hedge"] <= plain["hedge"], case
        assert margin["hedge_cost_eur"] >= plain["hedge_cost_eur"], case
        flipped += plain["hedge"] and not margin["hedge"]
    assert flipped


def test_property_breakeven_separates_hedge_from_hold():
    for case in cases():
        r = decide(**case)
        if r["breakeven_p_fail"] is None:
            assert not r["hedge"], case
        elif abs(case["p"] - r["breakeven_p_fail"]) > 1e-5:
            assert r["hedge"] == (case["p"] > r["breakeven_p_fail"]), case


def test_remainder_is_its_own_action():
    r = decide()
    deposit, remainder = r["actions"]
    assert (deposit["step"], deposit["amount_eur"]) == ("deposit", 240)
    assert (remainder["step"], remainder["amount_eur"]) == ("remainder", 960)
    assert remainder == r["remainder"]
    assert remainder["action"] == "purchase_order" and remainder["order_value_eur"] == 1200
    assert remainder["amount_source"]["value"] == 960
    assert {"fact_id": "f-price"} in remainder["amount_source"]["inputs"]
    contracts.validate("coverage", {"covered": 0, "needed": 0, "unit": "x", "sources": [remainder["amount_source"]]})
    limit = 1000        # a synthetic signer limit
    assert remainder["amount_eur"] < limit < r["order_value_eur"]


def test_remainder_stands_without_a_hedge_and_on_its_own():
    r = decide(p=0)
    assert r["actions"] == [] and r["remainder"]["amount_eur"] == 960
    alone = hedge.remainder_action(Input(1200, SCENARIO), Input("20%", SCENARIO))
    assert alone["amount_eur"] == 960


def test_order_without_a_deposit_and_deposit_of_everything():
    r = decide(p=1, model="full_order", deposit=None)
    assert [a["step"] for a in r["actions"]] == ["order"] and r["remainder"] is None
    paid = decide(p=1, deposit=1200)
    assert [a["step"] for a in paid["actions"]] == ["deposit"] and paid["remainder"]["amount_eur"] == 0


@pytest.mark.parametrize("text,value", [("25%", 300), ("25 % deposit", 300), ("12,5 percent", 150),
                                        ("€1,113", 1113), ("1 113 EUR", 1113), ("eur 240.50", 240.5)])
def test_deposit_is_parsed_from_quoted_text(text, value):
    assert decide(deposit=text)["commitment_eur"] == value


@pytest.mark.parametrize("text", ["150%", "20% now, 30% later", "$240", "half", "240"])
def test_deposit_that_cannot_be_read_is_refused(text):
    with pytest.raises(ModelError):
        decide(deposit=text)


@pytest.mark.parametrize("text,grouped,value", [
    ("17 003", False, 17003), ("17 003", False, 17003), ("17 003,50", False, 17003.5), ("2,10", False, 2.1),
    ("0.125", False, 0.125), ("1.234,56", False, 1234.56), ("1,234,567", False, 1234567),
    ("17,003", True, 17003), ("17.003", True, 17003), ("2.1", True, 2.1),
])
def test_numbers_are_parsed_in_code(text, grouped, value):
    assert hedge.parse_number(text, grouped) == value


@pytest.mark.parametrize("text", ["1,250", "1.250", "1 2 3", "ten", "", True])
def test_ambiguous_numbers_are_refused(text):
    with pytest.raises(ModelError):
        hedge.parse_number(text)


@pytest.mark.parametrize("kw", [dict(model="deposit_only"), dict(model="full_order", salvage=100),
                                dict(goal=MARGIN), dict(model="full_order", deposit=None, goal=MARGIN)])
def test_drilldown_lists_every_input_with_its_source(kw):
    r = decide(**kw)
    inputs, steps = r["drilldown"]["inputs"], r["drilldown"]["computed"]
    expected = {"p_fail", "copies_at_risk", "loss_per_copy", "units", "units_per_copy", "unit_price", "cost_model"}
    if kw.get("deposit", 240) is not None:
        expected.add("deposit")
    expected |= {name for name, key in (("salvage", "salvage"), ("protect_margin", "goal")) if kw.get(key)}
    assert {i["name"] for i in inputs} == expected
    for i in inputs:
        assert i["source"].startswith(("fact ", "goal: ", "scenario input: ")), i
        assert all(ref in r["sources"] for ref in i["refs"])
    known = expected | {s["name"] for s in steps}
    used = {name for s in steps for name in s["from"]}
    assert used <= known
    assert expected - used <= {"cost_model"}        # every input reaches a computed row, the model picks the formula
    assert computed(r, "commitment")["value"] == r["commitment_eur"]
    json.dumps(r, allow_nan=False)


@pytest.mark.parametrize("source", [{}, [], {"label": "somebody said"}, {"fact_id": ""}, "f-1"])
def test_an_input_without_a_source_is_refused(source):
    with pytest.raises(ModelError):
        Input(1, source)


@pytest.mark.parametrize("kw", [{"p": 1.2}, {"p": float("nan")}, {"copies": -1}, {"loss": None}, {"units": 0},
                                {"per_copy": 0}, {"price": True}, {"price": float("inf")}])
def test_bad_numbers_are_refused(kw):
    with pytest.raises(ModelError):
        decide(**kw)


def test_loss_comes_from_the_goal_and_is_never_guessed():
    given = hedge.goal_loss({"statement": "All on time.", "unit_loss_eur": 50})
    assert given.value == 50 and given.refs[0]["goal_line"].startswith("All on time.")
    assert hedge.goal_loss({"unit_loss_by_edition": {"Deluxe": 70, "Plain": 0}}, "deluxe").value == 70
    for goal, edition in (({}, None), ({"unit_loss_eur": -1}, None), ({"unit_loss_by_edition": {"A": 1}}, None),
                          ({"unit_loss_eur": 5, "edition": "A"}, "B")):
        with pytest.raises(ModelError):
            hedge.goal_loss(goal, edition)


def test_p_fail_comes_from_the_reliability_card():
    facts = [{"fact_id": f"{n}:{name}", "fact_type": "po_line", "subject": {"po_number": str(n), "po_line": 1},
              "attribute": name, "value": value}
             for n in range(4)
             for name, value in (("supplier_id", "S1"), ("promised_date", "2030-01-10"),
                                 ("actual_delivery_date", "2030-01-12" if n == 0 else "2030-01-09"))]
    card = reliability.card(reliability.history(facts), "S1")
    given = hedge.p_fail(card)
    assert given.value == pytest.approx(1 - card["score"]["value"])
    assert any(ref.get("fact_id") for ref in given.refs)
    worse = hedge.p_fail(reliability.update(card, "broken", "promise-1"))
    assert worse.value > given.value
    with pytest.raises(ModelError):
        hedge.p_fail(reliability.card(reliability.history([]), "S2"))


def test_supplier_without_history_cites_the_prior():
    card = reliability.card(reliability.history([]), "S2", scenario_prior={"value": 0.5, "label": "new supplier"})
    given = hedge.p_fail(card)
    assert given.value == 0.5 and any(r.get("scenario_input") == "new supplier" for r in given.refs)


@pytest.mark.parametrize("p,kind", [(0.3, "hedge"), (0.001, "hold")])
def test_recommendation_fits_the_contract(p, kind):
    rec = hedge.recommendation(decide(p=p), "PART-A", supplier_id="A", backup_supplier_id="B", offer_id="O-1")
    contracts.validate("recommendation", rec)
    assert rec["kind"] == kind and rec["hedge"]["p_fail_source"] == FACT
