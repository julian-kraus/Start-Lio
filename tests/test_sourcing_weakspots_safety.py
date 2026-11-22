"""A weak spot must not disappear because of a price, a wording or a document its own supplier sent."""

import random

import pytest

from sourcing import weakspots
from sourcing.facts import Facts
from test_sourcing_weakspots import PRICE, list_source, ranked, restock, spot, status, world  # noqa: F401


@pytest.mark.parametrize("deluxe, basic, exposure", [
    (90, 90, 45000), (91, 90, 45000), (90, 89, 44500), (90, 20, 10000), (90, 0, 0),
])
def test_a_displaced_copy_keeps_its_own_editions_value(world, deluxe, basic, exposure):
    card = spot(ranked(world, {"unit_loss_by_edition": {"Deluxe": deluxe, "Basic": basic}}), "CARD")
    assert card["exposure_eur"] == exposure
    assert card["detail"]["status"] == ("at_risk" if exposure else "covered")
    editions = card["detail"]["editions"]
    assert exposure == sum(e["copies_lost"]["value"] * e["value_per_copy_eur"]["value"] for e in editions)
    if exposure and deluxe != basic:
        assert "500 Basic copies" in card["reason"]


def test_the_lenders_own_shortage_keeps_its_value(world):
    restock(world, "CARD", 1400)                        # 1600 short: Deluxe 1000 and Basic 600 before lending
    card = spot(ranked(world, {"unit_loss_by_edition": {"Deluxe": 90, "Basic": 20}}), "CARD")
    assert card["exposure_eur"] == 1600 * 20


@pytest.mark.parametrize("editions", [2, 3])
def test_exposure_never_falls_when_one_value_rises(world, editions):
    rng = random.Random(editions)
    names = ["P-A", "P-B", "P-C"][:editions]
    if editions == 3:
        world.add("plan_edition", {"product_id": "P-C"}, edition_name="Plain", demand=900, rate_per_hour=100)
        world.add("packout", {"date": "2030-01-06", "product_id": "P-C"}, units_packed=100, site_id="HUB",
                  po_line="line Plain")
    base = list(world.current)
    for case in range(150):
        world.current = list(base)
        world.drop("bom_line", child_id="CARD")
        for product in names:
            world.bom(product, "CARD", rng.choice([1, 1, 2, 3, 0.1]))
            world.add("plan_edition", {"product_id": product}, path="ops/split.csv",
                      preorders=rng.randint(100, 2000))
        restock(world, "CARD", rng.randint(0, 4000))
        world.drop("x_workbook_row", record_id="memo")
        if first := rng.choice([None, "Deluxe", "Basic"]):
            world.add("x_workbook_row", {"record_id": "memo"}, note=f"Priority by memo: {first} first.")
        facts, coverage = Facts(world.current), world.run()
        values = {product: rng.choice([0, 1, 20, 89, 90, 91, rng.randint(0, 100)]) for product in names}
        before = weakspots.rank(facts, coverage, {"unit_loss_by_edition": values})
        for product in names:
            for rise in (0.01, 1, 5, 100):
                raised = {**values, product: values[product] + rise}
                after = weakspots.rank(facts, coverage, {"unit_loss_by_edition": raised})
                for low in before:
                    high = spot(after, low["part_id"])
                    assert high["exposure_eur"] >= low["exposure_eur"], (case, values, raised, low["part_id"])


@pytest.mark.parametrize("statement", [
    "All Basic copies can be delayed.",
    "All Basic copies are expendable.",
    "Give up all Basic copies.",
    "All Basic copies may ship next week.",
    "All Basic copies can be postponed, every other copy on time.",
    "All Basic copies on the back burner.",
    "All Deluxe and every other copy on time.",
    "All Deluxe on time, and everything else as well.",
    "All Deluxe on time. The same goes for retail.",
    "All Deluxe on time or not, I do not mind.",
    "All Basic on time. All Basic can wait.",
    "Every Gold copy on time.",
    "",
])
def test_wording_that_is_not_fully_recognised_values_every_edition_and_says_so(world, statement):
    values, line = weakspots.goal_values({"statement": statement, "unit_loss_eur": 7}, world.run()["editions"])
    assert values == {"P-A": 7, "P-B": 7} and "assumed" in line


def test_an_expendable_edition_does_not_hide_the_other_ones_risk(world):
    restock(world, "BOOK", 1400)
    book = spot(ranked(world, {"statement": "All Basic copies are expendable.", "unit_loss_eur": PRICE}), "BOOK")
    assert book["exposure_eur"] == 300 * PRICE and book["detail"]["status"] == "critical"
    assert any("assumed" in s.get("goal_line", "") for s in book["sources"])


@pytest.mark.parametrize("statement", [
    "All Deluxe on time.",
    "All Deluxe on time. Supplier goodwill can suffer. Don't overspend.",
    "Every Deluxe copy must ship on time, but all Basic copies can be delayed.",
])
def test_fully_recognised_wording_narrows_without_an_assumption(world, statement):
    values, line = weakspots.goal_values({"statement": statement, "unit_loss_eur": 7}, world.run()["editions"])
    assert values == {"P-A": 7, "P-B": 0} and "assumed" not in line


def test_one_edition_cannot_be_priced_twice(world):
    with pytest.raises(ValueError):
        weakspots.goal_values({"unit_loss_by_edition": {"Deluxe": 5, "P-A": 0}}, world.run()["editions"])


SENT = [f"suppliers/S4_Vendor/{folder}/letter.pdf"
        for folder in ("contracts", "notices", "order_confirmations", "documents", "any_new_folder", "inbox/older")]


@pytest.mark.parametrize("path", SENT)
def test_a_suppliers_document_cannot_list_a_source(world, path):
    list_source(world, "BOOK", "S4", path=path)
    assert status(ranked(world), "BOOK") == "critical"


@pytest.mark.parametrize("path", SENT)
def test_a_suppliers_document_cannot_qualify_a_source(world, path):
    list_source(world, "BOOK", "S4")
    world.add("supplier", {"supplier_id": "S4"}, as_of="2030-01-01", qualification_status="conditional")
    world.add("supplier", {"supplier_id": "S4"}, path=path, as_of="2030-01-05", qualification_status="qualified")
    book = spot(ranked(world), "BOOK")
    assert book["detail"]["status"] == "critical" and book["detail"]["not_qualified"] == ["S4"]


@pytest.mark.parametrize("path", SENT)
def test_a_suppliers_document_can_still_take_a_qualification_away(world, path):
    list_source(world, "BOOK", "S4")
    world.add("supplier", {"supplier_id": "S4"}, as_of="2030-01-01", qualification_status="qualified")
    world.add("supplier", {"supplier_id": "S4"}, path=path, as_of="2030-01-05", qualification_status="suspended")
    assert status(ranked(world), "BOOK") == "critical"


@pytest.mark.parametrize("path", ["suppliers/export.csv", "suppliers/S2_Vendor/profile.csv",
                                  "suppliers/S2_Vendor/profile.json"])
def test_an_export_under_suppliers_still_lists_a_source(world, path):
    list_source(world, "BOOK", "S2", path=path)
    assert status(ranked(world), "BOOK") != "critical"


def test_a_source_without_a_profile_is_not_called_qualified(world):
    world.drop("supplier", supplier_id="S3")
    reason = spot(ranked(world), "BOOK")["reason"]
    assert "one listed source (S3), qualification not on file" in reason and "qualified source" not in reason
