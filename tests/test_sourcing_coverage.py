"""Coverage domain checks on synthetic facts: two editions, one shared part, one day of packing."""

from datetime import datetime

import pytest

from plan import contracts
from sourcing import coverage
from sourcing.facts import Facts

NOW = datetime.fromisoformat("2030-01-07T05:00:00+00:00")        # a Monday
DEADLINE = datetime.fromisoformat("2030-01-07T18:00:00+00:00")
NEXT_DAY = datetime.fromisoformat("2030-01-08T10:00:00+00:00")


class World:
    def __init__(self):
        self.current, self.believed, self.issues = [], [], []
        for product, name, demand, rate in (("P-A", "Deluxe", 1100, 100), ("P-B", "Basic", 2100, 200)):
            self.add("plan_edition", {"product_id": product}, edition_name=name, demand=demand, rate_per_hour=rate)
            self.add("packout", {"date": "2030-01-06", "product_id": product}, units_packed=100, site_id="HUB",
                     po_line=f"line {name}")
        for product, part, qty in (("P-A", "CARD", 1), ("P-A", "BOOK", 2), ("P-B", "CARD", 1), ("P-B", "CASE", 1)):
            self.bom(product, part, qty)
        for part, qty in (("CARD", 2500), ("BOOK", 2000), ("CASE", 2000)):
            self.stock(part, qty)
        self.policy = self.add("x_workbook_row", {"record_id": "memo"}, note="Priority by memo: Deluxe first.")
        for weekday in ("Monday", "Tuesday"):
            self.add("shift", {"day": weekday}, shift="06:00-18:00", hours=12)
        self.add("route", {"route_id": "R1"}, origin="PORT", destination="HUB", mode="road", transit_days=1)

    def add(self, fact_type, subject, path="ops/file.csv", as_of=None, **attrs):
        out = {}
        for name, value in attrs.items():
            out[name] = f"f{len(self.current)}"
            self.current.append({"fact_id": out[name], "path": path, "fact_type": fact_type, "subject": subject,
                                 "attribute": name, "value": value, "as_of": as_of, "confidence": 0.9})
        return out

    def drop(self, fact_type, **match):
        self.current = [f for f in self.current if not (
            f["fact_type"] == fact_type and all(f["subject"].get(k) == v for k, v in match.items()))]

    def bom(self, product, part, qty):
        return self.add("bom_line", {"parent_id": product, "child_id": part}, qty_per=qty)

    def stock(self, part, qty, batch=None, **more):
        return self.add("stock_batch", {"batch": batch or f"B-{part}", "material_id": part}, qty=qty,
                        site_id="HUB", **more)

    def receipt(self, batch, part, qty, po="PO-1", supplier="S1", note="N1", **more):
        return self.add("goods_receipt", {"gr_number": batch}, material_id=part, qty_received=qty, po_number=po,
                        supplier_id=supplier, delivery_note=note, **more)

    def ship(self, qty=500, eta="2030-01-06", destination="PORT", ref="T1", part="CARD", status="in_transit",
             po="PO-1", **more):
        ids = self.add("shipment", {"shipment_id": ref}, reference=ref, eta=eta, destination=destination,
                       status=status, material_ids=part, po_numbers=po, **more)
        if qty is not None:
            ids.update(self.add("shipment_qty", {"reference": ref, "material_id": part}, qty_planned=qty))
        return ids

    def believe(self, rule, key, value, winning=(), losing=()):
        self.believed.append({"rule_id": rule, "key": key, "value": value, "winning_fact_ids": list(winning),
                              "losing_fact_ids": list(losing), "contradiction": bool(losing)})

    def run(self, now=NOW, deadline=DEADLINE, **kw):
        result = coverage.compute(Facts(self.current, self.believed, self.issues), now=now, deadline=deadline, **kw)
        contracts.validate("coverage", result)
        check_sources(result, {f["fact_id"] for f in self.current})
        return result


def check_sources(result, known):
    """Every number is a figure whose facts exist, and every fact id named anywhere exists."""
    for name in ("covered", "needed", "gap"):
        assert result["fact_ids"][name], name

    def walk(node, where):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "fact_ids" and isinstance(value, list):
                    assert set(value) <= known, where
                elif isinstance(value, (int, float)) and not isinstance(value, bool):
                    top = not where and key in ("covered", "needed", "gap")
                    assert top or (key == "value" and node.get("fact_ids")), f"{where}/{key} has no fact_ids"
                else:
                    walk(value, f"{where}/{key}")
        elif isinstance(node, list):
            for item in node:
                assert isinstance(item, bool) or not isinstance(item, (int, float)), f"bare number in {where}"
                walk(item, where)

    walk(result, "")
    assert {s["fact_id"] for s in result["sources"]} <= known


def part(result, name):
    return next(p for p in result["parts"] if p["material_id"] == name)


def edition(result, product):
    return next(e for e in result["editions"] if e["product_id"] == product)


def allocation(result, name):
    return {a["product_id"]: a["allocated"]["value"] for a in part(result, name)["allocation"]}


def alerts(result):
    return {a["kind"] for a in result["escalations"]}


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

NOTE = "fitted by supplier into BOOK; not ordered, received or stocked separately"


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


def revised(world, use_up="N"):
    change = world.add("change_record", {"change_id": "C1", "child_id": "CASE", "change_field": "drawing_rev"},
                       new="C", use_up_old=use_up)
    master = world.add("component", {"material_id": "CASE"}, drawing_rev="B")
    world.believe("drawing_rev", {"material_id": "CASE"}, "C", ["derived:" + change["new"]], [master["drawing_rev"]])
    world.drop("stock_batch", material_id="CASE")
    for batch, po, supplier, rev in (("NEW", "PO-C", "SC", "C"), ("OLD", "PO-B", "SB", "B")):
        world.stock("CASE", 1200, batch=batch)
        world.receipt(batch, "CASE", 1200, po=po, supplier=supplier, note=f"N-{batch}")
        world.add("po_line", {"po_number": po, "po_line": 1}, material_id="CASE", supplier_id=supplier)
        world.add("supplier_statement", {"po_number": po, "statement_no": 1}, revision=rev, value=f"rev {rev}")


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


def test_inbound_at_the_wrong_revision_does_not_count(world):
    revised(world)
    world.ship(part="CASE", po="PO-B")
    result = world.run()
    assert part(result, "CASE")["inbound"][0]["not_counted"] == "disputed_revision"
    assert "inbound_disputed_revision" in alerts(result)


def test_safety_stock_is_no_launch_buffer(world):
    world.drop("stock_batch", material_id="CARD")
    world.stock("CARD", 3000, safety_stock="900")
    card = part(world.run(), "CARD")
    assert (card["safety_stock"]["value"], card["safety_stock"]["launch_buffer"]) == (900, False)
    assert (card["usable"]["value"], card["gap"]["value"]) == (3000, 0)


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
