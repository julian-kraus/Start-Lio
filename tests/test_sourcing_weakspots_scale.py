"""A part shared by many editions: rank ends, and what it does not search it does not understate."""

import random
from itertools import permutations

import pytest

from sourcing import weakspots
from sourcing.facts import Facts
from test_sourcing_weakspots import ranked, restock, spot, world  # noqa: F401


@pytest.fixture(autouse=True)
def bounded_search(monkeypatch):
    def orders(users):
        assert len(users) <= 7, f"a search over every order of {len(users)} editions does not end"
        return permutations(users)
    monkeypatch.setattr(weakspots, "permutations", orders)


def share(world, count, qty=lambda i: 1):
    for i in range(count - 2):
        product = f"P-{i:02}"
        world.add("plan_edition", {"product_id": product}, edition_name=f"E{i}", demand=110, rate_per_hour=100,
                  preorders=60)
        world.add("packout", {"date": "2030-01-06", "product_id": product}, units_packed=10, site_id="HUB",
                  po_line=f"line {i}")
        world.bom(product, "CARD", qty(i))
    return ["P-A", "P-B"] + [f"P-{i:02}" for i in range(count - 2)]


@pytest.mark.parametrize("count", [8, 10, 40])
def test_many_editions_price_the_shortage_on_the_most_valuable_copies(world, count):
    products = share(world, count)
    restock(world, "CARD", 3000 + 100 * (count - 2) - 150)
    values = {p: 10 for p in products} | {"P-00": 90, "P-01": 50}
    card = spot(ranked(world, {"unit_loss_by_edition": values}), "CARD")
    assert card["detail"]["copies_at_risk"]["value"] == 150
    assert card["exposure_eur"] == card["detail"]["money_at_risk_eur"]["value"] == 100 * 90 + 50 * 50
    assert "lending not tried" in card["reason"] and not card["detail"]["substitute"]


def test_many_editions_without_a_shortage_have_no_exposure(world):
    share(world, 10)
    restock(world, "CARD", 3800)
    card = spot(ranked(world, {"edition": "all", "unit_loss_eur": 10}), "CARD")
    assert card["exposure_eur"] == 0 and card["detail"]["status"] == "ok"
    assert "lending" not in card["reason"]


def test_many_editions_exposure_never_falls_when_one_value_rises(world):
    rng = random.Random(10)
    products = share(world, 10, qty=lambda i: (1, 2, 3, 0.1)[i % 4])
    for _ in range(40):
        restock(world, "CARD", rng.randint(0, 4300))
        facts, coverage = Facts(world.current), world.run()
        values = {p: rng.choice([0, 1, 20, 89, 90]) for p in products}
        low = spot(weakspots.rank(facts, coverage, {"unit_loss_by_edition": values}), "CARD")
        for product in products:
            raised = {"unit_loss_by_edition": {**values, product: values[product] + 1}}
            assert spot(weakspots.rank(facts, coverage, raised), "CARD")["exposure_eur"] >= low["exposure_eur"]


def test_the_bound_is_never_below_the_searched_worst_order(world, monkeypatch):
    rng = random.Random(6)
    products = share(world, 6, qty=lambda i: (1, 2, 3, 0.1)[i])
    for case in range(80):
        restock(world, "CARD", rng.randint(0, 3700))
        facts, coverage = Facts(world.current), world.run()
        goal = {"unit_loss_by_edition": {p: rng.choice([0, 1, 20, 89, 90]) for p in products}}
        searched = spot(weakspots.rank(facts, coverage, goal), "CARD")
        with monkeypatch.context() as patch:
            patch.setattr(weakspots, "SEARCHED", 1)
            bound = spot(weakspots.rank(facts, coverage, goal), "CARD")
        assert bound["exposure_eur"] >= searched["exposure_eur"], case
        assert bound["detail"]["money_at_risk_eur"]["value"] >= searched["detail"]["money_at_risk_eur"]["value"], case
