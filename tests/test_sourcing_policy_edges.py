"""Coverage edge cases: which policy counts, who may clear a shortage, timed arrivals and each line's hours."""

from datetime import datetime

import pytest

from sourcing_world import NEXT_DAY, World, alerts, allocation, edition, part

EXPORT = "suppliers/S9/export.csv"          # our own export sits at the root of the supplier's folder


@pytest.fixture
def world():
    return World()


def directive(world, text, as_of, **more):
    return world.add("policy_directive", {"directive_no": as_of}, as_of=as_of, directive=text, **more)


def covered(result):
    return {e["product_id"]: e["covered"]["value"] for e in result["editions"]}


# which policy counts

@pytest.mark.parametrize("text", [
    "Priority: Basic before Deluxe", "Priority: Basic goes ahead of Deluxe", "New priority: Basic",
    "Basic now has priority over Deluxe", "PRIORITY for Basic",
])
def test_a_newer_policy_in_other_words_is_not_ignored(world, text):
    added = directive(world, text, "2030-01-07")
    result = world.run()
    assert result["priority"] == {"order": None, "fact_ids": [added["directive"]]}
    assert "priority_unknown" in alerts(result)


@pytest.mark.parametrize("until, first", [
    ("2030-01-06", "P-A"), ("2030-01-07T04:00:00+00:00", "P-A"), ("2030-01-07", "P-B"), ("2030-02-01", "P-B"),
])
def test_an_expired_directive_no_longer_sets_the_priority(world, until, first):
    directive(world, "Priority: Basic first", "2030-01-05", valid_until=until)
    assert world.run()["priority"]["order"][0] == first


def test_an_unreadable_expiry_is_not_guessed(world):
    directive(world, "Priority: Basic first", "2030-01-05", valid_until="until further notice")
    result = world.run()
    assert result["priority"]["order"] is None
    assert "priority_unknown" in alerts(result)


# who may clear a shortage

def clip(world, note, path, description=None):
    world.bom("P-A", "CLIP", 1)
    more = {"description": description} if description else {}
    world.add("supplier_part", {"supplier_id": "S9", "material_id": "CLIP"}, path=path, note=note, **more)
    return world.run()


def still_short(result):
    return not result["not_shortages"] and edition(result, "P-A")["covered"]["value"] == 0


@pytest.mark.parametrize("path", [
    "suppliers/S9/notices/n.pdf", "suppliers/S9/notices/parts.csv", "suppliers/S9/n.pdf", "mail/a/b",
    "mail/a/export.csv",
])
def test_what_a_supplier_sent_never_clears_a_shortage(world, path):
    result = clip(world, "phantom", path)
    assert still_short(result)
    assert "phantom_unclear" in alerts(result)


def test_our_export_in_the_supplier_folder_may_clear_one(world):
    result = clip(world, "phantom", EXPORT)
    assert [n["material_id"] for n in result["not_shortages"]] == ["CLIP"]


def test_a_supplier_cannot_teach_the_words_of_a_note(world):
    world.add("supplier_part", {"supplier_id": "S8", "material_id": "X"}, path="mail/a/b",
              description="procure separately")
    result = clip(world, "phantom, procure separately", EXPORT)
    assert still_short(result)
    assert "phantom_unclear" in alerts(result)


@pytest.mark.parametrize("note", [
    "phantom: 0", "phantom as of 2099-01-01", "phantom by 2099", "phantom = ✗", "phantom: ❌",
])
def test_digits_and_symbols_are_not_understood(world, note):
    result = clip(world, note, EXPORT)
    assert still_short(result)
    assert "phantom_unclear" in alerts(result)


def test_a_part_called_by_a_number_may_be_named(world):
    world.bom("P-A", "BAG-7", 1)
    world.stock("BAG-7", 5000)
    assert not still_short(clip(world, "phantom, installed by the vendor with BAG-7", EXPORT))


# arrivals

@pytest.mark.parametrize("eta", ["2030-01-07T23:00:00+00:00", "2030-01-07T20:00:00", "2030-01-07 19:00:00+01:00"])
def test_a_timed_eta_does_not_count_before_it_arrives(world, eta):
    world.drop("stock_batch", material_id="CARD")
    world.ship(qty=3000, eta=eta, destination="HUB")
    card = part(world.run(), "CARD")
    assert card["inbound"][0]["not_counted"] == "after_deadline"
    assert (card["inbound_counted"]["value"], card["gap"]["value"]) == (0, 3000)


@pytest.mark.parametrize("eta, at", [
    ("2030-01-07T09:30:00+00:00", "2030-01-07T09:30:00+00:00"),
    ("2030-01-07T10:30:00+01:00", "2030-01-07T09:30:00+00:00"),
    ("2030-01-07 00:00:00", "2030-01-07T06:00:00+00:00"),       # an exported date
])
def test_a_timed_eta_is_the_arrival(world, eta, at):
    world.ship(eta=eta, destination="HUB")
    assert part(world.run(), "CARD")["inbound"][0]["arrives_at"] == at


def test_the_road_leg_comes_after_a_timed_eta(world):
    world.ship(eta="2030-01-07T15:00:00+00:00")
    assert part(world.run(deadline=NEXT_DAY), "CARD")["inbound"][0]["not_counted"] == "after_deadline"


# each line's hours

@pytest.mark.parametrize("split", [[3000], [1500, 1500], [100] * 30, [400, 2600]])
def test_a_shared_part_is_limited_by_each_lines_hours(world, split):
    world.drop("stock_batch", material_id="CARD")
    for n, qty in enumerate(split):
        world.ship(qty=qty, eta="2030-01-07", ref=f"T{n}")
    result = world.run(deadline=NEXT_DAY)
    assert part(result, "CARD")["inbound_counted"]["value"] == 1200
    assert covered(result) == {"P-A": 400, "P-B": 800}
    assert allocation(result, "CARD") == {"P-A": 400, "P-B": 800}


def test_stock_before_the_arrival_goes_in_policy_order(world):
    world.drop("stock_batch", material_id="CARD")
    world.stock("CARD", 500)
    world.ship(qty=2400, eta="2030-01-07")
    assert covered(world.run(deadline=NEXT_DAY)) == {"P-A": 900, "P-B": 800}


def test_without_a_policy_no_line_gets_more_than_its_hours(world):
    world.drop("x_workbook_row", record_id="memo")
    world.drop("stock_batch", material_id="CARD")
    world.ship(qty=3000, eta="2030-01-07")
    got = covered(world.run(deadline=NEXT_DAY))
    assert got["P-A"] <= 400 and got["P-B"] <= 800


def test_a_limit_from_the_hours_cites_the_rate(world):
    world.drop("stock_batch", material_id="CARD")
    world.ship(qty=3000, eta="2030-01-07")
    result = world.run(deadline=NEXT_DAY)
    rate = next(f["fact_id"] for f in world.current
                if f["attribute"] == "rate_per_hour" and f["subject"]["product_id"] == "P-A")
    deluxe = edition(result, "P-A")
    assert deluxe["limited_by"] == ["CARD"]
    assert rate in deluxe["covered"]["fact_ids"]


# fractions

@pytest.mark.parametrize("qty", [0.1, 0.2, 0.05, 0.3, 0.7])
@pytest.mark.parametrize("policy", [True, False])
def test_a_fractional_qty_per_loses_no_copy(world, qty, policy):
    if not policy:
        world.drop("x_workbook_row", record_id="memo")
        world.stock("CARD", 500, batch="MORE")     # so that only BOOK could limit
    world.drop("bom_line", child_id="BOOK")
    world.bom("P-A", "BOOK", qty)
    world.drop("stock_batch", material_id="BOOK")
    world.stock("BOOK", round(1000 * qty, 2))
    result = world.run()
    assert part(result, "BOOK")["gap"]["value"] == 0
    assert edition(result, "P-A")["covered"]["value"] == 1000
