"""Added supply is split by what each line can still pack, as inbound is."""

from datetime import timedelta

import pytest

from plan import contracts
from sourcing import simulate
from sourcing.facts import Facts
from sourcing_world import DEADLINE, NEXT_DAY, NOW, World, allocation, edition, part

ARRIVAL = NOW + timedelta(days=1, hours=1)              # the next day's first shift, 4 h before its deadline


def supply(quantity, name="CARD", at=NOW, ref="O1", take=None, **more):
    body = {"offer_id": ref, "supplier_id": "S2", "part_id": name, "quantity": quantity,
            "available_at": at.isoformat(), "sources": [{"scenario_input": "test offer"}], **more}
    return {"kind": "offer", "offer": body, "quantity": take or quantity}


def sim(world, *actions, deadline=DEADLINE):
    result = simulate.simulate(Facts(world.current, world.believed, world.issues), list(actions),
                               now=NOW, deadline=deadline)
    contracts.validate("coverage", result)
    return result


def copies(result):
    return [edition(result, p)["covered"]["value"] for p in ("P-A", "P-B")]


@pytest.fixture
def world():
    world = World()
    world.drop("stock_batch", batch="B-CARD")           # none of the shared part on hand
    return world


def test_an_unrelated_supply_keeps_each_lines_hours_on_inbound(world):
    world.ship(qty=3000, eta="2030-01-07")
    before, after = sim(world, deadline=NEXT_DAY), sim(world, supply(100, "CASE"), deadline=NEXT_DAY)
    assert copies(before) == copies(after) == [400, 800]
    assert allocation(after, "CARD") == allocation(before, "CARD")


@pytest.mark.parametrize("policy", [True, False], ids=["policy", "no policy"])
@pytest.mark.parametrize("split", [(3000,), (1500, 1500), (100, 2900)])
def test_added_supply_is_split_like_the_same_units_inbound(world, policy, split):
    if not policy:
        world.drop("x_workbook_row", record_id="memo")
    offered = [supply(qty, at=ARRIVAL, ref=f"O{n}") for n, qty in enumerate(split)]
    by_offer = sim(world, *offered, deadline=NEXT_DAY)
    for n, qty in enumerate(split):
        world.ship(qty=qty, eta=ARRIVAL.isoformat(), destination="HUB", ref=f"T{n}")
    by_inbound = sim(world, deadline=NEXT_DAY)
    assert copies(by_offer) == copies(by_inbound)
    assert allocation(by_offer, "CARD") == allocation(by_inbound, "CARD")


def test_supply_after_inbound_gives_no_line_more_than_its_hours(world):
    world.ship(qty=100, eta=ARRIVAL.isoformat(), destination="HUB")
    result = sim(world, supply(2900, at=ARRIVAL), deadline=NEXT_DAY)
    assert copies(result) == [400, 800]
    assert allocation(result, "CARD") == {"P-A": 400, "P-B": 800}


def test_a_limit_from_the_hours_cites_the_rate(world):
    rate = next(f["fact_id"] for f in world.current
                if f["attribute"] == "rate_per_hour" and f["subject"]["product_id"] == "P-A")
    result = sim(world, supply(3000, at=ARRIVAL), deadline=NEXT_DAY)
    assert rate in edition(result, "P-A")["covered"]["fact_ids"]
    split = part(result, "CARD")["allocation"]
    assert rate in next(a for a in split if a["product_id"] == "P-A")["allocated"]["fact_ids"]


@pytest.mark.parametrize("qty", [0.05, 0.1, 0.2])
def test_a_fractional_qty_per_loses_no_copy(qty):
    world = World()
    world.drop("bom_line", parent_id="P-A", child_id="BOOK")
    world.bom("P-A", "BOOK", qty)
    world.drop("stock_batch", batch="B-BOOK")
    result = sim(world, supply(round(1000 * qty), "BOOK"))
    assert edition(result, "P-A")["covered"]["value"] == 1000


@pytest.mark.parametrize("existing", ["early", "late"])
def test_competing_takes_never_move_the_first_editions_units_past_its_hours(world, existing):
    takes = {"early": supply(1000, at=NOW, take=650, min_quantity=400),
             "late": supply(1000, at=NOW + timedelta(hours=7), take=800, min_quantity=400)}
    before = sim(world, takes[existing])
    after = sim(world, *takes.values())
    assert copies(before) == ([650, 0] if existing == "early" else [600, 200])
    assert copies(after) == [650, 0]                    # more units in all, but too late for the first line
    assert [a["feasible"] for a in after["actions"]] == [True, False]


def test_takes_of_two_parts_are_chosen_for_what_they_pack_together(world):
    world.drop("stock_batch", batch="B-BOOK")
    takes = [supply(1000, at=NOW, take=650, min_quantity=400),
             supply(1000, at=NOW + timedelta(hours=7), take=800, min_quantity=400),
             supply(800, "BOOK", ref="O2")]
    assert copies(sim(world, *takes)) == [400, 400]     # books bind the first line, so the larger take helps
