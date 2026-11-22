"""Coverage edge cases: believed values from the real kb engine, split shipments, the clock, worded facts."""

from datetime import datetime
from pathlib import Path

import pytest
import yaml

from kb import engine
from sourcing_world import NEXT_DAY, World, alerts, edition, part

SEED = yaml.safe_load((Path(__file__).resolve().parent.parent / "kb" / "rules.seed.yaml").read_text())


def believe_as_kb(world):
    """Replace the hand-written believed values by what the seed rules give on the same facts."""
    records = engine.build_records([
        engine.Fact(f["fact_id"], f["fact_type"], f["subject"], f["attribute"], f["value"], f["as_of"],
                    f["confidence"], "test", f["path"], f["path"])
        for f in world.current])
    believed, _, _ = engine.evaluate(SEED["rules"], records, SEED["settings"],
                                     datetime.fromisoformat("2030-01-07T05:00:00+00:00"))
    world.believed = [vars(b) for b in believed]


def unsure(world, added, confidence):
    for fact in world.current:
        if fact["fact_id"] in added.values():
            fact["confidence"] = confidence


@pytest.fixture
def world():
    return World()


# inspection

@pytest.mark.parametrize("confidence", [0.4, 0.9])
def test_a_failed_lot_is_blocked_however_sure_the_verdict(world, confidence):
    world.stock("BOOK", 500, batch="BAD")
    unsure(world, world.add("inspection_result", {"gr_number": "BAD"}, decision="fail"), confidence)
    believe_as_kb(world)
    result = world.run()
    book = part(result, "BOOK")
    assert (book["usable"]["value"], book["blocked"]["value"]) == (2000, 500)
    assert "failed_inspection" in alerts(result)


# receipts

@pytest.mark.parametrize("confidence", [0.4, 0.9])
@pytest.mark.parametrize("lines, usable, blocked_if_unsure", [
    ([2000], 2000, 0),              # one line for both bookings
    ([1000, 1000], 2000, 0),        # one line per booking
    ([1600], 1600, 400),
    ([1000], 1000, 1000),           # equals one booking, so kb sees no contradiction
])
def test_a_note_is_compared_with_all_its_bookings(world, confidence, lines, usable, blocked_if_unsure):
    world.drop("stock_batch", material_id="BOOK")
    for batch in ("GR-1", "GR-2"):
        world.stock("BOOK", 1000, batch=batch)
        world.receipt(batch, "BOOK", 1000)
    for line, qty in enumerate(lines):
        scan = world.add("delivery_note_line", {"delivery_note": "N1", "line": line}, material_id="BOOK",
                         final_qty=qty)
        unsure(world, scan, confidence)
    believe_as_kb(world)
    book = part(world.run(), "BOOK")
    assert book["usable"]["value"] == usable
    assert book["blocked"]["value"] == (blocked_if_unsure if confidence < 0.6 else 0)


def test_several_note_lines_below_the_bookings_wait_for_a_decision(world):
    world.receipt("B-BOOK", "BOOK", 2000)
    for line, qty in enumerate((900, 800)):
        world.add("delivery_note_line", {"delivery_note": "N1", "line": line}, material_id="BOOK", final_qty=qty)
    believe_as_kb(world)
    result = world.run()                    # kb compares single lines, so no believed total exists
    book = part(result, "BOOK")
    assert (book["usable"]["value"], book["blocked"]["value"]) == (1700, 300)
    assert "disputed_receipt" in alerts(result)


@pytest.mark.parametrize("confidence", [0.4, 0.9])
@pytest.mark.parametrize("ok, bad_booked, bad_left, count, failed_usable", [
    (1000, 500, 500, 900, 400),         # the shortfall may all sit in the good lot
    (None, 500, 100, 300, 1900),        # the held lot cannot absorb more than is left of it
    (None, 500, 500, 300, 2000),        # the held lot absorbs all of it
])
def test_a_failed_inspection_never_raises_usable_stock(confidence, ok, bad_booked, bad_left, count, failed_usable):
    def usable(failed):
        world = World()
        if ok:
            world.drop("stock_batch", material_id="BOOK")
            world.stock("BOOK", ok, batch="OK")
            world.receipt("OK", "BOOK", ok)
        world.stock("BOOK", bad_left, batch="BAD")
        world.receipt("BAD", "BOOK", bad_booked)
        scan = world.add("delivery_note_line", {"delivery_note": "N1", "line": 1}, material_id="BOOK",
                         final_qty=count)
        unsure(world, scan, confidence)
        if failed:
            world.add("inspection_result", {"gr_number": "BAD"}, decision="fail")
        believe_as_kb(world)
        return part(world.run(), "BOOK")["usable"]["value"]

    assert usable(True) == failed_usable
    assert usable(True) <= usable(False)


# pack hours and the clock

@pytest.mark.parametrize("split", [[2000], [1000, 1000], [2500, 2500], [100, 5000], [100, 100, 5000]])
def test_the_pack_hour_limit_does_not_depend_on_the_split(world, split):
    world.drop("stock_batch", material_id="CARD")
    for n, qty in enumerate(split):
        world.ship(qty=qty, eta="2030-01-07", ref=f"T{n}")
    result = world.run(deadline=NEXT_DAY)
    assert part(result, "CARD")["inbound_counted"]["value"] == 1200
    assert result["covered"] == 1200


@pytest.mark.parametrize("split", [[2400], [1200, 1200]])
def test_a_split_shipment_creates_no_coverage_after_stock_ran_out(world, split):
    world.drop("stock_batch", material_id="CARD")
    world.stock("CARD", 500)
    for n, qty in enumerate(split):
        world.ship(qty=qty, eta="2030-01-07", ref=f"T{n}")
    result = world.run(deadline=NEXT_DAY)
    assert (result["covered"], result["gap"]) == (1700, 1300)


def test_part_of_a_transit_day_is_a_day(world):
    world.drop("route")
    world.add("route", {"route_id": "R1"}, origin="PORT", destination="HUB", mode="road", transit_days=0.9)
    world.ship(eta="2030-01-07")
    assert part(world.run(), "CARD")["inbound"][0]["not_counted"] == "after_deadline"


def test_later_arrivals_share_the_pack_hours_with_earlier_ones(world):
    world.drop("stock_batch", material_id="CARD")
    world.add("shift", {"day": "Wednesday"}, shift="06:00-18:00", hours=12)
    world.ship(qty=4000, eta="2030-01-07", ref="T1")        # Tuesday: 400 are left for Wednesday
    world.ship(qty=5000, eta="2030-01-08", ref="T2")
    card = part(world.run(deadline=datetime.fromisoformat("2030-01-09T10:00:00+00:00")), "CARD")
    assert [i["counts"]["value"] for i in card["inbound"]] == [4000, 800]


@pytest.mark.parametrize("hour, status, destination, eta, arrives, counts", [
    ("17:30", "arrived_port", "PORT", "2030-01-06", "08T00:00", 0),     # still at the port: a day of road ahead
    ("19:00", "arrived_port", "PORT", "2030-01-06", "08T00:00", 0),
    ("17:00", "in_transit", "PORT", "2030-01-06", "07T17:00", 300),     # on the road: not earlier than now
    ("17:00", "in_transport", "PORT", "2030-01-06", "07T17:00", 300),
    ("19:00", "in_transit", "HUB", "2030-01-05", "07T19:00", 0),        # overdue and past the deadline
])
def test_undelivered_inbound_cannot_arrive_before_now(world, hour, status, destination, eta, arrives, counts):
    world.drop("stock_batch", material_id="CARD")
    world.ship(qty=5000, eta=eta, status=status, destination=destination)
    result = world.run(now=datetime.fromisoformat(f"2030-01-07T{hour}:00+00:00"))
    card = part(result, "CARD")
    assert card["inbound"][0]["arrives_at"] == f"2030-01-{arrives}:00+00:00"
    assert card["inbound_counted"]["value"] == counts
    assert result["covered"] == counts


def test_nothing_counts_once_no_pack_hour_is_left(world):
    world.drop("stock_batch", material_id="CARD")
    world.stock("CARD", 9000)               # it never runs out
    world.drop("shift", day="Tuesday")
    world.ship(eta="2030-01-07")
    assert part(world.run(deadline=NEXT_DAY), "CARD")["inbound"][0]["not_counted"] == "after_pack_hour"


# phantom wording

def clip(world, *notes):
    world.bom("P-A", "CLIP", 4)
    for n, note in enumerate(notes):
        world.add("supplier_part", {"supplier_id": f"S{n}", "material_id": "CLIP"}, note=note)
    return world.run()


@pytest.mark.parametrize("notes", [
    ["not a phantom, must be ordered"], ["Not, as first thought, a phantom"], ["isn't a phantom"],
    ["isn’t installed by the vendor"], ["phantom: no"], ["no longer phantom: buy separately"],
    ["was a phantom until the redesign"], ["Is this a phantom?"], ["not a phantom; not stocked separately"],
    ["must not be forgotten, it is stocked separately"], ["phantom", "not a phantom"],
    ["phantom status withdrawn"], ["phantom flag removed, order separately"], ["phantom cancelled"],
    ["ceased to be a phantom"], ["phantom in error"], ["wrongly marked phantom"], ["phantom, but buy separately"],
    ["phantom status ended"],
    # wording no doubt list would think of: only a note understood word for word clears a shortage
    ["phantom status void"], ["phantom: to be procured on its own"], ["phantom line deleted"],
    ["treat as a normal part, phantom tag is stale"], ["phantom, but procure separately"],
    ["phantom in the old BOM only"], ["phantom flag under review"], ["phantom tag to be confirmed"],
    ["phantom, pending confirmation"], ["ex-phantom part"], ["phantom superseded by revision C"],
    ["phantom, purchase separately"], ["phantom status suspended"], ["phantom assumption unverified"],
    ["phantom claim is doubtful"], ["phantom. Procure it as a spare"], ["Phantom. Besorgung separat"],
])
def test_a_negated_or_doubtful_note_never_hides_a_shortage(world, notes):
    result = clip(world, *notes)
    assert result["not_shortages"] == []
    assert edition(result, "P-A")["covered"]["value"] == 0


@pytest.mark.parametrize("note", [
    "phantom", "Installed by the vendor with BOOK", "phantom, not stocked separately",
    "mounted by our vendor, never bought or stocked on its own",
])
def test_a_phantom_known_only_from_wording_is_escalated(world, note):
    result = clip(world, note)
    assert result["not_shortages"][0]["material_id"] == "CLIP"
    assert "phantom_from_note" in alerts(result)


def test_a_doubtful_phantom_note_is_escalated(world):
    assert "phantom_unclear" in alerts(clip(world, "not a phantom, must be ordered"))


def test_a_phantom_believed_by_kb_needs_no_escalation(world):
    line = world.bom("P-A", "CLIP", 4)
    world.believe("phantom_part", {"material_id": "CLIP"}, True, [line["qty_per"]])
    assert "phantom_from_note" not in alerts(world.run())


# priority wording

def reworded(world, text):
    for fact in world.current:
        if fact["fact_id"] == world.policy["note"]:
            fact["value"] = text
    return world.run()


@pytest.mark.parametrize("text, first", [
    ("Priority change: Basic waits, Deluxe first.", "P-A"),
    ("Priority change: Deluxe waits, Basic first.", "P-B"),
    ("Priority change: Deluxe waits, Basic goes first", "P-B"),
    ("Priority: Deluxe (P-A) first then Basic", "P-A"),
    ("Priority for shared parts; Basic second; Deluxe goes first", "P-A"),
])
def test_the_edition_said_to_be_first_is_first(world, text, first):
    assert reworded(world, text)["priority"]["order"][0] == first


@pytest.mark.parametrize("text", [
    "Priority: Basic is not first", "Priority: Deluxe first, Basic first",
    "Priority: Basic waits until Deluxe is packed first", "Priority as discussed for Basic. Who is first?",
    "Priority: is Basic first?",
    "Priority: previously Deluxe first, now Basic", "Priority: Deluxe first, now Basic",
    "Priority: Deluxe first, Basic instead",
    "Priority: Deluxe first is superseded", "Priority: Deluxe first is void", "Priority: Deluxe first rule suspended",
    "Priority: Deluxe first, replaced by Basic", "Priority: Deluxe first, Basic takes over",
    "Priority: Deluxe first. Update: Basic has priority", "Priority: old rule Deluxe first; new rule Basic ahead",
    "Priority: Deluxe first on paper, Basic leads", "Priority: Deluxe at first, Basic from today",
    "Priority: Deluxe first. Withdrawn by the board", "Outdated priority: Deluxe first",
    "Priority: whoever is first, Deluxe",
])
def test_an_unclear_priority_is_escalated_not_guessed(world, text):
    result = reworded(world, text)
    assert result["priority"]["order"] is None
    assert "priority_unknown" in alerts(result)


def test_a_priority_known_only_from_wording_is_escalated(world):
    result = world.run()
    assert result["priority"]["order"] and "priority_from_note" in alerts(result)


def test_an_unclear_newer_priority_does_not_fall_back_to_the_older(world):
    world.add("policy_directive", {"directive_key": "d"}, as_of="2030-01-07", directive="Priority: Basic not first")
    assert world.run()["priority"]["order"] is None


def test_a_name_inside_a_longer_name_is_not_a_second_edition(world):
    for fact in world.current:
        if fact["value"] == "Basic":
            fact["value"] = "Deluxe Light"
    assert reworded(world, "Priority: Deluxe Light first")["priority"]["order"][0] == "P-B"


@pytest.mark.parametrize("text, order", [
    ("Priority: Deluxe first, then Mini, then Basic", ["P-A", "P-C", "P-B"]),
    ("Priority: Deluxe first, Basic waits, Mini as usual", None),
])
def test_three_editions_need_the_whole_sequence(world, text, order):
    world.add("plan_edition", {"product_id": "P-C"}, edition_name="Mini", demand=100, rate_per_hour=50)
    world.bom("P-C", "CARD", 1)
    assert reworded(world, text)["priority"]["order"] == order
