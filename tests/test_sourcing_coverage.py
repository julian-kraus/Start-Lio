"""Coverage on synthetic facts: need, allocation, phantom lines, usable stock, the contract."""

from datetime import datetime

import pytest

from sourcing_world import World, alerts, allocation, check_sources, edition, part, revised


@pytest.fixture
def world():
    return World()


# need

def test_need_is_open_copies_times_qty_per(world):
    result = world.run()
    assert [edition(result, p)["open"]["value"] for p in ("P-A", "P-B")] == [1000, 2000]
    assert part(result, "BOOK")["need"]["value"] == 2000
    assert part(result, "CARD")["need"]["value"] == 3000
    assert result["needed"] == 3000


def test_packout_log_wins_over_the_reported_packed_count(world):
    world.add("plan_edition", {"product_id": "P-A"}, path="ops/status.csv", packed=300)
    result = world.run()
    assert edition(result, "P-A")["open"]["value"] == 1000
    assert "packed_differs_from_log" in alerts(result)


def test_expired_bom_line_is_ignored(world):
    world.add("bom_line", {"parent_id": "P-A", "child_id": "OLD"}, qty_per=1, valid_to="2029-12-31")
    assert "OLD" not in [p["material_id"] for p in world.run()["parts"]]


@pytest.mark.parametrize("effective, need", [("2030-01-07", 3000), ("2030-01-09", 2000)])
def test_qty_per_change_applies_from_its_effective_date(world, effective, need):
    subject = {"parent_id": "P-A", "child_id": "BOOK", "change_id": "C1"}
    world.add("change_record", subject, effective_from=effective)
    world.add("change_record", {**subject, "change_field": "qty_per"}, new="3")
    assert part(world.run(), "BOOK")["need"]["value"] == need


# shared parts

def test_policy_allocates_the_shared_part(world):
    result = world.run()
    assert allocation(result, "CARD") == {"P-A": 1000, "P-B": 1500}
    assert (result["covered"], result["gap"]) == (2500, 500)
    assert world.policy["note"] in edition(result, "P-B")["covered"]["fact_ids"]


def test_switching_the_policy_fact_switches_the_allocation(world):
    for f in world.current:
        if f["fact_id"] == world.policy["note"]:
            f["value"] = "Priority by memo: Basic first."
    result = world.run()
    assert allocation(result, "CARD") == {"P-A": 500, "P-B": 2000}
    assert edition(result, "P-A")["covered"]["value"] == 500


def test_newer_policy_wins(world):
    world.add("policy_directive", {"directive_key": "d"}, as_of="2030-01-07", directive="Priority: Basic first")
    assert world.run()["priority"]["order"] == ["P-B", "P-A"]


def test_a_supplier_cannot_set_the_priority(world):
    world.add("x_document_line", {"line_key": "l"}, path="suppliers/s1/mail.txt", as_of="2030-01-07",
              text="Priority: Basic first")
    assert world.run()["priority"]["order"] == ["P-A", "P-B"]


@pytest.mark.parametrize("conflict", [False, True])
def test_without_one_policy_the_split_is_in_proportion_and_escalates(world, conflict):
    if conflict:
        world.add("policy_directive", {"directive_key": "d"}, directive="Priority: Basic first")
    else:
        world.drop("x_workbook_row")
    result = world.run()
    assert allocation(result, "CARD") == {"P-A": 833, "P-B": 1666}
    assert "priority_unknown" in alerts(result)


def test_what_a_limited_edition_cannot_use_goes_to_the_next(world):
    world.drop("stock_batch", material_id="BOOK")
    world.stock("BOOK", 800)
    result = world.run()
    assert allocation(result, "CARD")["P-A"] == 1000                # still reserved
    assert [edition(result, p)["covered"]["value"] for p in ("P-A", "P-B")] == [400, 2000]
    assert edition(result, "P-A")["limited_by"] == ["BOOK"]


# phantom lines

NOTE = "pre-assembled by the vendor with BOOK; never stocked on its own"


def test_phantom_line_from_a_fact_is_not_a_shortage(world):
    world.bom("P-A", "CLIP", 4)
    note = world.add("supplier_part", {"supplier_id": "S1", "material_id": "CLIP"}, note=NOTE)
    result = world.run()
    assert "CLIP" not in [p["material_id"] for p in result["parts"]]
    assert result["not_shortages"][0]["fact_ids"] == [note["note"]]
    assert edition(result, "P-A")["covered"]["value"] == 1000


@pytest.mark.parametrize("via", ["believed", "issue"])
def test_phantom_line_from_kb(world, via):
    line = world.bom("P-A", "CLIP", 4)
    if via == "believed":
        world.believe("phantom_part", {"material_id": "CLIP"}, True, [line["qty_per"]])
    else:
        world.issues.append({"kind": "phantom_part", "payload": {"material_id": "CLIP"},
                             "fact_ids": [line["qty_per"]]})
    assert world.run()["not_shortages"][0]["material_id"] == "CLIP"


def test_a_stocked_part_with_such_a_note_is_no_phantom(world):
    world.add("supplier_part", {"supplier_id": "S1", "material_id": "BOOK"}, note=NOTE)
    assert world.run()["not_shortages"] == []


def test_a_part_without_stock_is_a_shortage(world):
    world.bom("P-A", "CLIP", 4)
    result = world.run()
    assert part(result, "CLIP")["gap"]["value"] == 4000
    assert edition(result, "P-A")["covered"]["value"] == 0


# usable stock

def test_believed_correction_of_a_receipt_wins(world):
    booked = world.receipt("B-BOOK", "BOOK", 2000)
    scan = world.add("delivery_note_line", {"delivery_note": "N1", "line": 1}, final_qty=200)
    world.believe("received_qty", {"delivery_note": "N1", "material_id": "BOOK"}, 200,
                  [scan["final_qty"]], [booked["qty_received"]])
    book = part(world.run(), "BOOK")
    assert (book["usable"]["value"], book["blocked"]["value"]) == (200, 0)
    assert scan["final_qty"] in book["usable"]["fact_ids"]


@pytest.mark.parametrize("value", [2000, None])
def test_an_unsure_lower_count_is_blocked_pending_decision(world, value):
    booked = world.receipt("B-BOOK", "BOOK", 2000)
    scan = world.add("delivery_note_line", {"delivery_note": "N1", "line": 1}, final_qty=200)
    world.believe("received_qty", {"delivery_note": "N1", "material_id": "BOOK"}, value,
                  [booked["qty_received"]], [scan["final_qty"]])
    result = world.run()
    book = part(result, "BOOK")
    assert (book["usable"]["value"], book["blocked"]["value"]) == (200, 1800)
    assert book["holds"][0]["status"] == "blocked pending decision"
    assert "disputed_receipt" in alerts(result)


def test_wrong_booking_counts_after_its_batch_was_packed(world):
    booked = world.receipt("GONE", "BOOK", 900)
    scan = world.add("delivery_note_line", {"delivery_note": "N1", "line": 1}, final_qty=600)
    world.believe("received_qty", {"delivery_note": "N1", "material_id": "BOOK"}, 600,
                  [scan["final_qty"]], [booked["qty_received"]])
    assert part(world.run(), "BOOK")["usable"]["value"] == 1700


@pytest.mark.parametrize("value", [0, None])
def test_failed_inspection_is_never_usable(world, value):
    lot = world.stock("BOOK", 500, batch="BAD")
    verdict = world.add("inspection_result", {"gr_number": "BAD"}, decision="fail")
    world.believe("batch_usable", {"batch": "BAD"}, value, [verdict["decision"]], [lot["qty"]])
    result = world.run()
    book = part(result, "BOOK")
    assert (book["booked"]["value"], book["usable"]["value"], book["blocked"]["value"]) == (2500, 2000, 500)
    assert "failed_inspection" in alerts(result)


def test_failed_lot_with_a_disputed_receipt_is_taken_out_once(world):
    lot = world.stock("BOOK", 500, batch="BAD")
    booked = world.receipt("BAD", "BOOK", 500)
    scan = world.add("delivery_note_line", {"delivery_note": "N1", "line": 1}, final_qty=300)
    world.believe("batch_usable", {"batch": "BAD"}, 0, [scan["final_qty"]], [lot["qty"]])
    world.believe("received_qty", {"delivery_note": "N1", "material_id": "BOOK"}, 500,
                  [booked["qty_received"]], [scan["final_qty"]])
    assert part(world.run(), "BOOK")["usable"]["value"] == 2000


def test_disputed_revision_is_blocked_and_escalated(world):
    revised(world)
    world.stock("CASE", 70, batch="UNPROVEN")
    result = world.run()
    case = part(result, "CASE")
    assert (case["usable"]["value"], case["blocked"]["value"]) == (1200, 1270)
    assert "disputed_revision" in alerts(result)


def test_old_revision_that_may_be_used_up_is_usable(world):
    revised(world, use_up="Y")
    assert part(world.run(), "CASE")["usable"]["value"] == 2400


def test_safety_stock_is_no_launch_buffer(world):
    world.drop("stock_batch", material_id="CARD")
    world.stock("CARD", 3000, safety_stock="900")
    card = part(world.run(), "CARD")
    assert (card["safety_stock"]["value"], card["safety_stock"]["launch_buffer"]) == (900, False)
    assert (card["usable"]["value"], card["gap"]["value"]) == (3000, 0)


# contract and inputs

def test_output_validates_and_every_figure_has_fact_ids(world):
    world.ship()
    result = world.run()                    # run() applies the contract and check_sources
    assert result["unit"] == "copies"


def test_the_source_check_catches_a_number_without_facts(world):
    result = world.run()
    result["editions"][0]["open"]["fact_ids"] = []
    with pytest.raises(AssertionError, match="no fact_ids"):
        check_sources(result, {f["fact_id"] for f in world.current})


def test_same_facts_in_another_order_give_the_same_result(world):
    first = world.run()
    world.current.reverse()
    assert world.run() == first


def test_bad_inputs_are_refused(world):
    with pytest.raises(ValueError, match="UTC offset"):
        world.run(now=datetime(2030, 1, 7))
    with pytest.raises(ValueError, match="no launch plan demand"):
        world.run(products=["P-Z"])


def test_asking_for_one_edition_does_not_free_what_the_policy_reserves():
    world = World()
    every = world.run()
    for product in ("P-A", "P-B"):
        one = world.run(products=[product])
        assert one["editions"] == [edition(every, product)]
        assert part(one, "CARD") == part(every, "CARD")
        assert one["covered"] == edition(every, product)["covered"]["value"]
        assert one["needed"] == edition(every, product)["open"]["value"]


def test_one_edition_reports_only_its_own_parts_and_escalations():
    world = World()
    world.drop("bom_line", parent_id="P-A", child_id="BOOK")
    world.bom("P-A", "SLEEVE", 1)
    world.ship(eta=None, part="SLEEVE")
    result = world.run(products=["P-B"])
    assert {p["material_id"] for p in result["parts"]} == {"CARD", "CASE"}
    assert {a.get("product_id", "P-B") for a in result["escalations"]} == {"P-B"}
    assert "SLEEVE" not in {a.get("material_id") for a in result["escalations"]}


def test_an_empty_selection_is_refused():
    with pytest.raises(ValueError):
        World().run(products=[])
