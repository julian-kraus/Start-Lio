"""Property: adding a feasible supply action never lowers copies."""

import random
from datetime import timedelta

import pytest

from sourcing import simulate
from sourcing.coverage_windows import Window
from sourcing.facts import Facts
from sourcing_world import DEADLINE, NOW, World

PARTS = ("CARD", "BOOK", "CASE")


def random_world(rng, equal_share=True):
    world = World()
    for name in PARTS:
        world.drop("stock_batch", batch=f"B-{name}")
        world.stock(name, rng.randint(0, 3000))
    if not equal_share:
        world.drop("bom_line", child_id="CARD")
        for product in ("P-A", "P-B"):
            world.bom(product, "CARD", rng.choice((1, 2, 3)))
    if rng.random() < 0.3:
        world.drop("x_workbook_row", record_id="memo")
    if rng.random() < 0.4:
        world.ship(qty=rng.randint(1, 2000), eta="2030-01-06", part=rng.choice(PARTS))
    if rng.random() < 0.3:
        lot = world.stock("BOOK", 700, batch="LOT")
        report = world.add("inspection_result", {"gr_number": "LOT"}, decision="reject")
        world.believe("batch_usable", {"batch": "LOT"}, 0, [report["decision"]], [lot["qty"]])
    return Facts(world.current, world.believed, world.issues)


def random_action(rng):
    at = (NOW + timedelta(minutes=rng.randint(-60, 840))).isoformat()
    sources = [{"scenario_input": "random supply"}]
    if rng.random() < 0.2:
        return {"kind": "conditional_release", "part_id": "BOOK", "batch": "LOT",
                "quantity": rng.randint(1, 700), "available_at": at, "sources": sources}
    quantity = rng.randint(1, 2500)
    return {"kind": "offer", "quantity": rng.randint(1, quantity), "offer": {
        "offer_id": f"O{rng.randint(1, 4)}", "supplier_id": "S2", "part_id": rng.choice(PARTS),
        "quantity": quantity, "available_at": at, "sources": sources}}


def one_more(seed, equal_share):
    """Coverage before and after one more action is put at a random position."""
    rng = random.Random(seed)
    facts = random_world(rng, equal_share)
    actions = [random_action(rng) for _ in range(rng.randint(0, 3))]
    before = simulate.simulate(facts, actions, now=NOW, deadline=DEADLINE)
    actions.insert(rng.randint(0, len(actions)), random_action(rng))
    return before, simulate.simulate(facts, actions, now=NOW, deadline=DEADLINE)


def copies(result):
    return {e["product_id"]: e["covered"]["value"] for e in result["editions"]}


@pytest.mark.parametrize("batch", range(10))
def test_adding_a_supply_never_lowers_copies(batch):
    for seed in range(batch * 300, (batch + 1) * 300):
        before, after = one_more(seed, equal_share=True)
        assert after["covered"] >= before["covered"], seed


@pytest.mark.parametrize("batch", range(10))
def test_adding_a_supply_never_lowers_copies_in_policy_order(batch):
    for seed in range(batch * 300, (batch + 1) * 300):
        before, after = one_more(seed, equal_share=False)
        for old, new in zip(before["parts"], after["parts"]):
            assert new["available"]["value"] >= old["available"]["value"], seed
        old, new = copies(before), copies(after)
        if order := before["priority"]["order"]:
            assert [new[p] for p in order] >= [old[p] for p in order], seed
        else:
            assert all(new[p] >= old[p] for p in old), seed


def test_pack_hours_match_an_hour_by_hour_count():
    rng = random.Random(17)
    start = NOW
    windows = [Window(start, start + timedelta(hours=5), 5, ()),
               Window(start + timedelta(hours=8), start + timedelta(hours=12), 4, ())]
    open_hours = [*range(5), *range(8, 12)]
    for _ in range(5000):
        stock, burn = rng.randint(0, 900), rng.choice((50, 75, 100))
        arrivals = [(rng.randint(0, 11), rng.randint(1, 600)) for _ in range(rng.randint(1, 4))]
        left, used, starved = stock, 0, False
        for hour in range(12):
            waiting = any(h == hour for h, _ in arrivals)
            if waiting and used < burn * sum(1 for h in open_hours if h < hour):
                starved = True
            left += sum(q for h, q in arrivals if h == hour)
            if hour in open_hours:
                take = min(burn, left)
                left, used = left - take, used + take
        expected = used if starved else stock + sum(q for _, q in arrivals)
        got = simulate.packed_by_deadline(stock, [(start + timedelta(hours=h), q) for h, q in arrivals],
                                          burn, windows)
        assert got == expected, (stock, burn, arrivals)
