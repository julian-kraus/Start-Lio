"""Coverage on synthetic facts: inbound against the pack hour, and line capacity."""

from datetime import datetime

import pytest

from sourcing_world import NEXT_DAY, World, alerts, edition, part, revised


@pytest.fixture
def world():
    return World()


def test_inbound_at_the_wrong_revision_does_not_count(world):
    revised(world)
    world.ship(part="CASE", po="PO-B")
    result = world.run()
    assert part(result, "CASE")["inbound"][0]["not_counted"] == "disputed_revision"
    assert "inbound_disputed_revision" in alerts(result)


# inbound

def test_inbound_counts_after_eta_plus_last_mile(world):
    world.ship(eta="2030-01-06")
    card = part(world.run(), "CARD")
    assert card["inbound"][0]["arrives_at"] == "2030-01-07T06:00:00+00:00"
    assert (card["inbound_counted"]["value"], card["gap"]["value"]) == (500, 0)
    assert card["gap_on_stock"]["value"] == 500


def test_last_mile_pushes_inbound_past_the_deadline(world):
    world.ship(eta="2030-01-07")
    assert part(world.run(), "CARD")["inbound"][0]["not_counted"] == "after_deadline"


def test_no_last_mile_at_the_packing_site(world):
    world.ship(eta="2030-01-07", destination="HUB")
    assert part(world.run(), "CARD")["inbound_counted"]["value"] == 500


def test_waiting_for_pickup_still_needs_the_road_leg(world):
    world.ship(eta="2030-01-03", status="arrived_port")
    assert part(world.run(), "CARD")["inbound"][0]["not_counted"] == "after_deadline"
    next_day = part(world.run(deadline=NEXT_DAY), "CARD")["inbound"][0]
    assert next_day["arrives_at"] == "2030-01-08T06:00:00+00:00"


def test_after_the_part_ran_out_only_the_remaining_pack_hours_count(world):
    world.ship(qty=5000, eta="2030-01-07")
    late = part(world.run(deadline=NEXT_DAY), "CARD")["inbound"][0]
    assert (late["counts"]["value"], late["limited_by"]) == (1200, "pack_hour")


def test_before_the_part_runs_out_all_units_count(world):
    world.drop("stock_batch", material_id="CARD")
    world.stock("CARD", 4000)
    world.ship(qty=5000, eta="2030-01-07")
    assert part(world.run(deadline=NEXT_DAY), "CARD")["inbound_counted"]["value"] == 5000


def test_inbound_after_the_last_pack_hour_does_not_count(world):
    world.drop("shift", day="Tuesday")
    world.ship(eta="2030-01-07")
    assert part(world.run(deadline=NEXT_DAY), "CARD")["inbound"][0]["not_counted"] == "after_pack_hour"


def test_believed_shipment_quantity_wins(world):
    planned = world.ship(qty=500)
    sent = world.add("shipment_qty", {"reference": "T1", "material_id": "CARD"}, path="suppliers/advice.pdf",
                     qty_shipped=300)
    world.believe("shipment_qty", {"reference": "T1"}, 300, [sent["qty_shipped"]], [planned["qty_planned"]])
    assert part(world.run(), "CARD")["inbound_counted"]["value"] == 300


@pytest.mark.parametrize("ship, reason", [
    ({"status": "cancelled"}, "cancelled"), ({"status": "delivered"}, "already_received"),
    ({"eta": None}, "eta_unknown"), ({"destination": "ELSEWHERE"}, "last_mile_unknown"),
    ({"qty": None}, "qty_unknown"), ({"dock_slot": "refused"}, "dock_slot_refused"),
])
def test_inbound_that_does_not_count(world, ship, reason):
    world.ship(**ship)
    card = part(world.run(), "CARD")
    assert (card["inbound"][0]["not_counted"], card["inbound_counted"]["value"]) == (reason, 0)


def test_shipment_on_hold_does_not_count(world):
    world.ship(po="PO-1|PO-2")
    world.add("supplier_statement", {"po_number": "PO-2", "statement_no": 1}, kind="on_hold", value="on hold")
    result = world.run()
    assert part(result, "CARD")["inbound"][0]["not_counted"] == "on_hold"
    assert "inbound_on_hold" in alerts(result)


@pytest.mark.parametrize("slot, state", [(None, "unknown"), ("released", "released")])
def test_dock_slot_release_is_required_by_the_policy(world, slot, state):
    rule = world.add("policy_directive", {"directive_key": "s3"}, directive="requires a booked dock slot")
    world.ship(**({"dock_slot": slot} if slot else {}))
    result = world.run()
    entry = part(result, "CARD")["inbound"][0]
    assert entry["dock_slot"]["state"] == state
    assert rule["directive"] in entry["dock_slot"]["fact_ids"]
    assert ("dock_slot_release" in alerts(result)) == (state == "unknown")


# capacity

def test_capacity_slack_per_line(world):
    cap = edition(world.run(), "P-A")["capacity"]
    assert cap["line"] == "line Deluxe"
    assert [cap[k]["value"] for k in ("hours_needed", "hours_available", "slack_hours")] == [10, 12, 2]


def test_windows_come_from_the_shifts_and_end_at_the_deadline(world):
    result = world.run(deadline=NEXT_DAY)
    assert [w["hours"]["value"] for w in result["windows"]] == [12, 4]


def test_rate_above_the_best_observed_day_is_unproven(world):
    result = world.run()
    cap = edition(result, "P-A")["capacity"]
    assert (cap["planned_best_day"]["value"], cap["best_observed_day"]["value"]) == (1200, 100)
    assert "rate unproven" in cap["flags"] and "rate_unproven" in alerts(result)


def test_rate_seen_before_is_proven(world):
    world.add("packout", {"date": "2030-01-05", "product_id": "P-A"}, units_packed=1300)
    assert "rate unproven" not in edition(world.run(), "P-A")["capacity"]["flags"]


def test_hours_already_gone_make_capacity_short(world):
    result = world.run(now=datetime.fromisoformat("2030-01-07T12:00:00+00:00"))
    cap = edition(result, "P-A")["capacity"]
    assert cap["slack_hours_now"]["value"] == -4
    assert "capacity short" in cap["flags"]
