"""simulate(actions) on synthetic facts."""

import copy
from datetime import timedelta

import pytest

from plan import contracts
from sourcing import coverage, simulate
from sourcing.facts import Facts
from sourcing_world import DEADLINE, NEXT_DAY, NOW, World, allocation, edition, part, revised

INPUT = [{"scenario_input": "test offer"}]


@pytest.fixture
def world():
    return World()


def offer(quantity=500, part_id="CARD", at=NOW, ref="O1", take=None, **more):
    body = {"offer_id": ref, "supplier_id": "S2", "part_id": part_id, "quantity": quantity, "sources": INPUT}
    if at is not None:
        body["available_at"] = at.isoformat()
    body.update(more)
    return {"kind": "offer", "offer": body, **({} if take is None else {"quantity": take})}


def failed_lot(world, part_id="BOOK", good=1400, held=600):
    world.drop("stock_batch", batch=f"B-{part_id}")
    world.stock(part_id, good, batch="GOOD")
    stock = world.stock(part_id, held, batch="LOT")
    report = world.add("inspection_result", {"gr_number": "LOT"}, decision="reject")
    world.believe("batch_usable", {"batch": "LOT"}, 0, [report["decision"]], [stock["qty"]])


def release(quantity=600, at=NOW, **more):
    return {"kind": "conditional_release", "part_id": "BOOK", "batch": "LOT", "quantity": quantity,
            "available_at": at.isoformat(), "sources": [{"scenario_input": "sorted lot"}], **more}


def sim(world, *actions, now=NOW, deadline=DEADLINE, **kw):
    result = simulate.simulate(Facts(world.current, world.believed, world.issues), list(actions),
                               now=now, deadline=deadline, **kw)
    contracts.validate("coverage", result)
    check_figures(result, {f["fact_id"] for f in world.current})
    return result


def check_figures(result, known):
    """Every computed number names facts that exist, or the labelled input it came from."""
    def walk(node, where):
        if isinstance(node, dict):
            assert set(node.get("fact_ids", []) if isinstance(node.get("fact_ids"), list) else []) <= known, where
            for key, value in node.items():
                if key in ("inputs", "sources"):
                    continue
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    plain = (not where and key in ("covered", "needed", "gap")) or key == "index"
                    sourced = key == "value" and (node.get("fact_ids") or node.get("inputs"))
                    assert plain or sourced, f"{where}/{key}"
                else:
                    walk(value, f"{where}/{key}")
        elif isinstance(node, list):
            for item in node:
                walk(item, where)

    walk(result, "")


def only(result):
    (action,) = result["actions"]
    return action


def in_policy_order(result):
    by_id = {e["product_id"]: e["covered"]["value"] for e in result["editions"]}
    return [by_id[p] for p in result["priority"]["order"] or by_id]


# no action

@pytest.mark.parametrize("setup", [
    lambda w: None, lambda w: w.ship(), failed_lot, revised,
    lambda w: w.drop("x_workbook_row", record_id="memo"),
], ids=["stock", "inbound", "failed lot", "revision", "no policy"])
def test_empty_list_equals_coverage(world, setup):
    setup(world)
    facts = Facts(world.current, world.believed, world.issues)
    assert sim(world) == coverage.compute(facts, now=NOW, deadline=DEADLINE)


def test_empty_list_equals_coverage_for_chosen_products(world):
    assert sim(world, products=["P-A"]) == world.run(products=["P-A"])


def test_only_infeasible_actions_leave_coverage_as_it_is(world):
    result = sim(world, offer(status="declined"))
    del result["actions"]
    assert result == world.run()


# supply

def test_an_offer_closes_a_gap_and_keeps_its_source(world):
    result = sim(world, offer(500))
    assert (result["covered"], result["gap"]) == (3000, 0)
    card = part(result, "CARD")
    assert (card["added"]["value"], card["available"]["value"], card["gap"]["value"]) == (500, 3000, 0)
    assert allocation(result, "CARD") == {"P-A": 1000, "P-B": 2000}
    assert only(result)["counts"]["inputs"] == INPUT
    assert INPUT[0] in result["sources"]


def test_a_fact_source_is_carried_into_the_result(world):
    fact = world.add("offer", {"offer_ref": "O1"}, qty=500)["qty"]
    result = sim(world, offer(sources=[{"fact_id": fact}]))
    assert fact in only(result)["quantity"]["fact_ids"]
    assert fact in result["fact_ids"]["covered"]
    assert {"fact_id": fact} in result["sources"]


def test_part_of_an_offer_can_be_taken(world):
    assert sim(world, offer(500, take=200))["covered"] == 2700


def test_the_next_limiting_part_binds(world):
    world.drop("stock_batch", batch="B-CASE")
    world.stock("CASE", 1600)
    result = sim(world, offer(500, part_id="CASE"), offer(900, ref="O2"))
    assert edition(result, "P-B")["covered"]["value"] == 2000
    world.drop("stock_batch", batch="B-BOOK")
    world.stock("BOOK", 1000)
    result = sim(world, offer(900, ref="O2"))
    assert edition(result, "P-A")["limited_by"] == ["BOOK"]


def test_a_supplier_part_number_is_resolved(world):
    world.add("part_xref", {"supplier_id": "S2", "supplier_part_no": "X-9"}, material_id="CARD")
    assert sim(world, offer(part_id="X-9"))["covered"] == 3000


def test_lead_time_counts_from_now(world):
    action = only(sim(world, offer(at=None, lead_time_h=3)))
    assert action["arrives_at"] == (NOW + timedelta(hours=3)).isoformat()


def test_nothing_lands_before_now(world):
    action = only(sim(world, offer(at=NOW - timedelta(days=2))))
    assert action["arrives_at"] == NOW.isoformat()


def test_the_caller_keeps_their_actions(world):
    actions = [offer(), offer(ref="O2", status="declined")]
    before = copy.deepcopy(actions)
    sim(world, *actions)
    assert actions == before


# expired and other infeasible actions

@pytest.mark.parametrize("more", [
    {"valid_until": (NOW - timedelta(seconds=1)).isoformat()},
    {"valid_until": NOW.isoformat()},
    {"status": "expired"},
], ids=["past", "this second", "marked expired"])
def test_an_expired_offer_is_infeasible(world, more):
    result = sim(world, offer(**more))
    assert (only(result)["feasible"], only(result)["reason"]) == (False, "expired")
    assert result["covered"] == 2500


def test_an_offer_still_valid_now_is_feasible_even_if_it_lands_later(world):
    action = offer(at=NOW + timedelta(hours=5), valid_until=(NOW + timedelta(hours=1)).isoformat())
    assert only(sim(world, action))["feasible"] is True


def test_validity_is_compared_across_utc_offsets(world):
    assert only(sim(world, offer(valid_until="2030-01-07T05:30:00+01:00")))["reason"] == "expired"


def test_an_expired_release_is_infeasible(world):
    failed_lot(world)
    assert only(sim(world, release(valid_until=NOW.isoformat())))["reason"] == "expired"


@pytest.mark.parametrize("action, reason", [
    (offer(status="declined"), "declined"),
    (offer(at=DEADLINE), "after_deadline"),
    (offer(at=None, lead_time_h=13), "after_deadline"),
    (offer(at=None, lead_time_h=10 ** 9), "after_deadline"),
    (offer(at=None), "arrival_unknown"),
    (offer(part_id="NOPE"), "part_not_in_plan"),
    (offer(take=501), "more_than_offered"),
    (offer(take=100, min_quantity=200), "below_min_quantity"),
    (offer(sources=[{"fact_id": "gone"}]), "unknown_fact"),
    (offer(sources=[]), "invalid_action"),
    (offer(take=0), "invalid_action"),
    (offer(take="500"), "invalid_action"),
    (offer(take=True), "invalid_action"),
    (offer(valid_until="2030-01-09T00:00:00"), "invalid_action"),
    ({"kind": "teleport"}, "invalid_action"),
    ({"kind": ["offer"]}, "invalid_action"),
    ({"kind": "offer"}, "invalid_action"),
    ("offer", "invalid_action"),
])
def test_infeasible_actions_say_why_and_add_nothing(world, action, reason):
    result = sim(world, action)
    assert (only(result)["feasible"], only(result)["reason"]) == (False, reason)
    assert result["covered"] == 2500


def test_an_offer_without_a_quantity_is_infeasible(world):
    action = offer()
    del action["offer"]["quantity"]
    assert only(sim(world, action))["reason"] == "qty_unknown"


def test_the_same_offer_is_not_counted_twice(world):
    result = sim(world, offer(300), offer(300))
    assert result["covered"] == 2800
    assert result["actions"][1]["reason"] == "already_used"


def test_two_takes_share_one_offer(world):
    result = sim(world, offer(400, take=300), offer(400, take=300))
    assert [a["quantity"]["value"] for a in result["actions"]] == [300, 100]


def test_an_offer_already_inbound_is_not_counted_again(world):
    world.ship(ref="O1", qty=100)
    assert only(sim(world, offer(100)))["reason"] == "already_inbound"


def test_supply_at_a_disputed_revision_is_infeasible(world):
    revised(world)
    assert only(sim(world, offer(part_id="CASE", supplier_id="SB")))["reason"] == "disputed_revision"
    assert only(sim(world, offer(part_id="CASE", supplier_id="SC")))["feasible"] is True


# conditional release

def test_a_release_returns_held_units(world):
    failed_lot(world)
    assert edition(sim(world), "P-A")["covered"]["value"] == 700
    result = sim(world, release(600))
    assert edition(result, "P-A")["covered"]["value"] == 1000
    assert only(result)["quantity"]["fact_ids"]


def test_a_release_needs_a_failed_lot(world):
    assert only(sim(world, release()))["reason"] == "nothing_to_release"


def test_a_release_cannot_exceed_what_is_held(world):
    failed_lot(world)
    assert only(sim(world, release(601)))["reason"] == "more_than_held"
    result = sim(world, release(400), release(400))
    assert [a["quantity"]["value"] for a in result["actions"]] == [400, 200]


def test_a_release_needs_a_time(world):
    failed_lot(world)
    action = release()
    del action["available_at"]
    assert only(sim(world, action))["reason"] == "arrival_unknown"


def test_a_release_does_not_waive_a_disputed_receipt(world):
    failed_lot(world)
    booked = world.receipt("LOT", "BOOK", 600, note="N9")["qty_received"]
    note = world.add("delivery_note", {"delivery_note": "N9"}, qty="60")["qty"]
    world.believe("received_qty", {"material_id": "BOOK", "delivery_note": "N9"}, 600, [booked], [note])
    assert only(sim(world, release()))["reason"] == "disputed_receipt"


def test_a_release_does_not_waive_a_disputed_revision(world):
    failed_lot(world)
    change = world.add("change_record", {"change_id": "C2", "child_id": "BOOK", "change_field": "drawing_rev"},
                       new="C", use_up_old="N")
    master = world.add("component", {"material_id": "BOOK"}, drawing_rev="B")
    world.believe("drawing_rev", {"material_id": "BOOK"}, "C", [change["new"]], [master["drawing_rev"]])
    assert only(sim(world, release()))["reason"] == "disputed_revision"


def test_a_disputed_receipt_is_not_releasable(world):
    booked = world.receipt("B-BOOK", "BOOK", 2000)["qty_received"]
    note = world.add("delivery_note", {"delivery_note": "N1"}, qty="200")["qty"]
    world.believe("received_qty", {"material_id": "BOOK", "delivery_note": "N1"}, 2000, [booked], [note])
    action = {**release(), "batch": "B-BOOK"}
    assert only(sim(world, action))["reason"] == "nothing_to_release"


# pack hours and priority

def test_supply_after_the_part_ran_out_only_fills_the_hours_left(world):
    world.drop("stock_batch", batch="B-CARD")
    world.stock("CARD", 300)                            # 300 an hour from 06:00, out at 07:00
    action = only(sim(world, offer(2700, at=DEADLINE - timedelta(hours=2))))
    assert (action["counts"]["value"], action["limited_by"]) == (600, "pack_hour")


def test_two_late_supplies_share_the_hours_left(world):
    world.drop("stock_batch", batch="B-CARD")
    world.stock("CARD", 300)
    late = DEADLINE - timedelta(hours=2)
    result = sim(world, offer(500, at=late), offer(500, at=late, ref="O2"))
    assert [a["counts"]["value"] for a in result["actions"]] == [500, 100]


@pytest.mark.parametrize("stock, limited", [(300, True), (2400, True), (3600, False)])
def test_an_offer_counts_like_the_same_units_inbound(stock, limited):
    by_offer, by_inbound = World(), World()
    for w in (by_offer, by_inbound):
        w.drop("stock_batch", batch="B-CARD")
        w.stock("CARD", stock)                          # Monday takes 3600, the rest is the next morning
    by_inbound.ship(qty=2700, eta="2030-01-08", destination="HUB")
    landed = part(by_inbound.run(deadline=NEXT_DAY), "CARD")["inbound"][0]
    at = NOW.fromisoformat(landed["arrives_at"])
    action = only(sim(by_offer, offer(2700, at=at), deadline=NEXT_DAY))
    assert action["counts"]["value"] == landed["counts"]["value"]
    assert (landed.get("limited_by") == "pack_hour") is limited


def test_supply_follows_the_priority_policy(world):
    world.drop("stock_batch", batch="B-BOOK")
    world.stock("BOOK", 1000)
    before, after = sim(world), sim(world, offer(1000, part_id="BOOK"))
    assert (in_policy_order(before), in_policy_order(after)) == ([500, 2000], [1000, 1500])
    assert after["covered"] == before["covered"]


def test_priority_may_cost_total_copies_when_it_uses_more_of_a_shared_part(world):
    world.drop("bom_line", parent_id="P-A", child_id="CARD")
    world.bom("P-A", "CARD", 2)
    world.drop("stock_batch", batch="B-BOOK")
    world.stock("BOOK", 1000)
    before, after = sim(world), sim(world, offer(1000, part_id="BOOK"))
    assert in_policy_order(after) > in_policy_order(before)
    assert after["covered"] < before["covered"]


def test_without_a_policy_the_extra_is_shared_by_need(world):
    world.drop("x_workbook_row", record_id="memo")
    assert allocation(sim(world, offer(200)), "CARD") == {"P-A": 900, "P-B": 1800}


def test_the_next_day_counts_towards_a_later_deadline(world):
    action = only(sim(world, offer(at=NEXT_DAY - timedelta(hours=1)), deadline=NEXT_DAY))
    assert action["feasible"] is True


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 10 ** 400])
def test_a_quantity_that_is_no_number_is_infeasible(world, bad):
    failed_lot(world)
    assert only(sim(world, release(bad)))["reason"] == "invalid_action"


def test_a_kind_that_is_no_text_is_not_echoed(world):
    assert only(sim(world, {"kind": 5}))["kind"] is None
