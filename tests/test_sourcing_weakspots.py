"""Weak-spot ranking on synthetic facts: two editions, a shared part and one exclusive part each."""

import copy

import pytest

from plan import contracts
from sourcing import weakspots
from sourcing.facts import Facts
from sourcing_world import World

PRICE = 100
DELUXE = {"statement": "Every Deluxe copy on time.", "unit_loss_eur": PRICE}
BASIC = {"statement": "Every Basic copy on time.", "unit_loss_eur": PRICE}
EVERY = {"edition": "all", "unit_loss_eur": PRICE}


@pytest.fixture
def world():
    """Open copies: Deluxe 1000, Basic 2000 of which 1000 are beyond pre-orders. CARD is 500 short."""
    w = World()
    w.add("plan_edition", {"product_id": "P-A"}, preorders=1100)
    w.add("plan_edition", {"product_id": "P-B"}, preorders=1100)
    for part, suppliers in (("CARD", ("S1", "S2")), ("BOOK", ("S3",)), ("CASE", ("S1", "S2"))):
        for supplier in suppliers:
            list_source(w, part, supplier)
    for supplier in ("S1", "S2", "S3"):
        w.add("supplier", {"supplier_id": supplier}, status="active", qualification_status="qualified")
    return w


def list_source(w, part, supplier, path="ops/file.csv"):
    w.add("supplier_part", {"material_id": part, "supplier_id": supplier}, path=path, primary="Y")


def restock(w, part, qty):
    w.drop("stock_batch", material_id=part)
    w.stock(part, qty)


def ranked(w, goal=DELUXE):
    coverage = w.run()
    spots = weakspots.rank(Facts(w.current, w.believed, w.issues), coverage, goal)
    known = {f["fact_id"] for f in w.current}
    for spot in spots:
        contracts.validate("weak_spot", spot)
        check_sources(spot, known)
    assert [s["rank"] for s in spots] == list(range(1, len(spots) + 1))
    return spots


def check_sources(spot, known):
    cited = {s["fact_id"] for s in spot["sources"] if "fact_id" in s}
    assert cited <= known

    def walk(node):
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            if "value" in node:
                assert node.get("fact_ids") or node.get("goal_line"), node
            assert set(node.get("fact_ids", [])) <= known
            for key, value in node.items():
                assert key == "value" or isinstance(value, str) or not isinstance(value, (int, float)), (key, node)
                walk(value)

    walk(spot["detail"])
    assert any(s.get("goal_line") for s in spot["sources"])


def spot(spots, part):
    return next(s for s in spots if s["part_id"] == part)


def status(spots, part):
    return spot(spots, part)["detail"]["status"]


def test_exposure_is_copies_at_risk_times_the_editions_value(world):
    restock(world, "BOOK", 1400)                        # two per copy: 300 copies cannot be packed
    book = spot(ranked(world), "BOOK")
    assert book["detail"]["copies_at_risk"]["value"] == 300
    assert book["exposure_eur"] == 300 * PRICE


def test_a_fractional_quantity_adds_no_copy(world):
    world.drop("bom_line", child_id="BOOK")
    world.bom("P-A", "BOOK", 0.1)
    restock(world, "BOOK", 70)
    assert spot(ranked(world), "BOOK")["detail"]["copies_at_risk"]["value"] == 300


def test_each_edition_is_priced_at_its_own_value(world):
    restock(world, "BOOK", 1400)
    restock(world, "CASE", 1800)
    spots = ranked(world, {"unit_loss_by_edition": {"Deluxe": 90, "P-B": "20"}})
    assert spot(spots, "BOOK")["exposure_eur"] == 300 * 90
    assert spot(spots, "CASE")["exposure_eur"] == 200 * 20


def test_rank_is_money_at_risk_times_no_substitute(world):
    restock(world, "BOOK", 1400)
    restock(world, "CARD", 1400)                        # 1600 short, 1000 retail copies can absorb
    spots = ranked(world, {"unit_loss_by_edition": {"Deluxe": 90, "Basic": 20}})
    assert [s["exposure_eur"] for s in spots] == sorted((s["exposure_eur"] for s in spots), reverse=True)
    card = spot(spots, "CARD")["detail"]
    assert 0 < card["no_substitute"]["value"] < 1
    for s in spots:
        money, factor = s["detail"]["money_at_risk_eur"]["value"], s["detail"]["no_substitute"]["value"]
        assert s["exposure_eur"] == pytest.approx(money * factor, abs=money * 0.0001 + 0.01)


def test_retail_copies_lend_a_shared_part_so_it_ranks_low(world):
    restock(world, "BOOK", 1900)
    spots = ranked(world)
    card = spot(spots, "CARD")
    assert card["detail"]["money_at_risk_eur"]["value"] == 500 * PRICE      # would outrank BOOK without lending
    assert card["detail"]["money_at_risk_eur"]["value"] > spot(spots, "BOOK")["exposure_eur"]
    assert card["exposure_eur"] == 0 and card["detail"]["status"] == "covered"
    assert card["rank"] > spot(spots, "BOOK")["rank"]
    assert "Basic copies beyond pre-orders" in card["reason"]


def test_only_copies_beyond_preorders_lend(world):
    world.drop("plan_edition", product_id="P-B")
    world.add("plan_edition", {"product_id": "P-B"}, edition_name="Basic", demand=2100, rate_per_hour=200,
              preorders=1900)                           # 200 beyond pre-orders
    card = spot(ranked(world), "CARD")
    assert card["exposure_eur"] == 300 * PRICE and "after lending" in card["reason"]


def test_packed_copies_serve_preorders_first(world):
    world.drop("plan_edition", product_id="P-B")
    world.add("plan_edition", {"product_id": "P-B"}, edition_name="Basic", demand=2100, rate_per_hour=200,
              preorders=50)                             # fewer than the 100 packed, so every open copy is retail
    facts = Facts(world.current)
    edition = next(e for e in world.run()["editions"] if e["product_id"] == "P-B")
    assert weakspots.retail_copies(facts, edition)[0] == 2000


def test_without_a_preorder_fact_nothing_lends(world):
    world.current = [f for f in world.current if f["attribute"] != "preorders"]
    assert spot(ranked(world), "CARD")["exposure_eur"] == 500 * PRICE


def test_a_supplier_document_cannot_free_copies_for_lending(world):
    world.current = [f for f in world.current if f["attribute"] != "preorders"]
    world.add("plan_edition", {"product_id": "P-B"}, path="mail/claim.eml", preorders=0)
    assert spot(ranked(world), "CARD")["exposure_eur"] == 500 * PRICE


def test_a_lender_gives_no_more_units_than_it_holds(world):
    world.drop("plan_edition", product_id="P-B")
    world.add("plan_edition", {"product_id": "P-B"}, edition_name="Basic", demand=2100, rate_per_hour=200,
              preorders=0)
    restock(world, "CARD", 100)                         # 2900 short and only 100 units on hand to lend
    assert spot(ranked(world), "CARD")["exposure_eur"] == 900 * PRICE


def test_the_policy_can_forbid_lending(world):
    world.drop("x_workbook_row", record_id="memo")
    world.add("x_workbook_row", {"record_id": "memo"}, note="Priority by memo: Basic first.")
    assert spot(ranked(world), "CARD")["exposure_eur"] == 500 * PRICE


def test_exclusive_part_of_the_protected_edition_with_one_source_is_critical(world):
    restock(world, "BOOK", 1400)
    spots = ranked(world)
    assert spots[0]["part_id"] == "BOOK" and status(spots, "BOOK") == "critical"


def test_single_source_is_critical_even_with_stock_on_hand(world):
    spots = ranked(world)
    book = spot(spots, "BOOK")
    assert book["detail"]["status"] == "critical" and book["exposure_eur"] == 0
    assert book["rank"] == 1 and "stock on hand" in book["reason"]


def test_a_second_qualified_source_is_not_critical(world):
    restock(world, "BOOK", 1400)
    list_source(world, "BOOK", "S2")
    assert status(ranked(world), "BOOK") == "at_risk"


@pytest.mark.parametrize("field, value", [("qualification_status", "conditional"), ("status", "blocked")])
def test_an_unqualified_second_source_does_not_count(world, field, value):
    list_source(world, "BOOK", "S4")
    world.add("supplier", {"supplier_id": "S4"}, **{field: value})
    book = spot(ranked(world), "BOOK")
    assert book["detail"]["status"] == "critical" and book["detail"]["not_qualified"] == ["S4"]


def test_the_newest_qualification_wins(world):
    list_source(world, "BOOK", "S4")
    world.add("supplier", {"supplier_id": "S4"}, as_of="2030-01-01", qualification_status="conditional")
    world.add("supplier", {"supplier_id": "S4"}, path="ops/new.csv", as_of="2030-01-05",
              qualification_status="qualified")
    assert status(ranked(world), "BOOK") != "critical"


def test_an_email_cannot_add_a_source(world):
    list_source(world, "BOOK", "S2", path="mail/offer.eml")
    assert status(ranked(world), "BOOK") == "critical"


def test_no_source_on_file_is_critical(world):
    world.drop("supplier_part", material_id="BOOK")
    book = spot(ranked(world), "BOOK")
    assert book["detail"]["status"] == "critical" and "no qualified source on file" in book["reason"]


def test_a_shared_part_with_one_source_is_not_critical(world):
    world.drop("supplier_part", material_id="CARD", supplier_id="S2")
    assert status(ranked(world), "CARD") == "covered"


def test_one_line_reason_per_part(world):
    restock(world, "BOOK", 1400)
    spots = ranked(world)
    assert sorted(s["part_id"] for s in spots) == ["BOOK", "CARD", "CASE"]
    for s in spots:
        assert s["reason"].endswith(".") and "\n" not in s["reason"]
    assert "300 Deluxe copies" in spot(spots, "BOOK")["reason"]
    assert "30,000 EUR" in spot(spots, "BOOK")["reason"]


def test_a_line_break_in_a_name_stays_one_line(world):
    for fact in world.current:
        if fact["value"] == "Deluxe":
            fact["value"] = "De\nluxe"
    restock(world, "BOOK", 1400)
    assert all("\n" not in s["reason"] for s in ranked(world, {"edition": "P-A", "unit_loss_eur": PRICE}))


def test_a_reason_says_when_the_goal_puts_no_value_on_the_copies(world):
    restock(world, "CASE", 1800)
    assert "the goal puts no value on them" in spot(ranked(world), "CASE")["reason"]


def test_a_phantom_line_is_no_weak_spot(world):
    world.bom("P-A", "MAGNET", 1)
    world.issues.append({"kind": "phantom_part", "payload": {"material_id": "MAGNET"}, "fact_ids": ["f0"]})
    assert "MAGNET" not in {s["part_id"] for s in ranked(world)}


def test_changing_the_goal_changes_the_ranking(world):
    restock(world, "BOOK", 1600)                        # 200 copies
    restock(world, "CASE", 1900)                        # 100 copies
    deluxe, basic = ranked(world, DELUXE), ranked(world, BASIC)
    assert [s["part_id"] for s in deluxe] != [s["part_id"] for s in basic]
    assert deluxe[0]["part_id"] == "BOOK" and spot(basic, "CASE")["exposure_eur"] == 100 * PRICE
    assert spot(basic, "BOOK")["exposure_eur"] == 0 and status(basic, "BOOK") != "critical"


def test_when_every_copy_counts_nothing_lends(world):
    assert spot(ranked(world, DELUXE), "CARD")["exposure_eur"] == 0
    assert spot(ranked(world, EVERY), "CARD")["exposure_eur"] == 500 * PRICE


def test_a_higher_value_scales_the_exposure(world):
    restock(world, "BOOK", 1400)
    low, high = ranked(world), ranked(world, {**DELUXE, "unit_loss_eur": 3 * PRICE})
    assert spot(high, "BOOK")["exposure_eur"] == 3 * spot(low, "BOOK")["exposure_eur"]


def test_a_goal_worth_nothing_protects_nothing(world):
    spots = ranked(world, {"unit_loss_eur": 0})
    assert all(s["exposure_eur"] == 0 and s["detail"]["status"] != "critical" for s in spots)


@pytest.mark.parametrize("goal, expected", [
    ({"unit_loss_eur": 7, "edition": "deluxe"}, (7, 0)),
    ({"unit_loss_eur": 7, "edition": "P-B"}, (0, 7)),
    ({"unit_loss_eur": 7, "edition": "both"}, (7, 7)),
    ({"unit_loss_eur": 7, "statement": "All Deluxe on time"}, (7, 0)),
    ({"unit_loss_eur": 7, "statement": "Everything on time"}, (7, 7)),
    ({"unit_loss_by_edition": {"Basic": 3}}, (0, 3)),
])
def test_goal_shapes(world, goal, expected):
    values, line = weakspots.goal_values(goal, world.run()["editions"])
    assert (values["P-A"], values["P-B"]) == expected and "value of a missing copy" in line


@pytest.mark.parametrize("goal", [
    {}, {"unit_loss_eur": None}, {"unit_loss_eur": -1}, {"unit_loss_eur": "a lot"},
    {"unit_loss_eur": float("nan")}, {"unit_loss_eur": float("inf")}, {"unit_loss_eur": 5, "edition": "Gold"},
    {"unit_loss_by_edition": {"Gold": 5}},
])
def test_no_price_or_edition_is_guessed(world, goal):
    with pytest.raises(ValueError):
        weakspots.goal_values(goal, world.run()["editions"])


def test_rank_is_pure(world):
    coverage, facts = world.run(), Facts(world.current)
    before = copy.deepcopy((coverage, DELUXE, world.current))
    assert weakspots.rank(facts, coverage, DELUXE) == weakspots.rank(facts, coverage, DELUXE)
    assert (coverage, DELUXE, world.current) == before


@pytest.mark.parametrize("product", ["P-A", "P-B"])
def test_a_report_narrowed_to_one_edition_still_prices_the_shared_part_over_all(world, product):
    facts, whole = Facts(world.current), spot(ranked(world), "CARD")
    narrow = weakspots.rank(facts, world.run(products=[product]), DELUXE)
    card = spot(narrow, "CARD")
    assert (card["exposure_eur"], card["detail"]) == (whole["exposure_eur"], whole["detail"])
    for s in narrow:
        contracts.validate("weak_spot", s)
