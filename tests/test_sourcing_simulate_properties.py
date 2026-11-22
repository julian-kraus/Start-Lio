"""Properties of adding a supply: copies in policy order never fall, as long as no offer has a minimum."""

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


def three_editions(demand, stock, uses):
    """P-A before P-B before P-C, with any bill of materials."""
    world = World()
    for kind in ("plan_edition", "packout", "bom_line", "stock_batch", "x_workbook_row"):
        world.drop(kind)
    for product, name in (("P-A", "Alpha"), ("P-B", "Beta"), ("P-C", "Gamma")):
        world.add("plan_edition", {"product_id": product}, edition_name=name, demand=demand[product],
                  rate_per_hour=100)
        world.add("packout", {"date": "2030-01-06", "product_id": product}, units_packed=0, site_id="HUB",
                  po_line=f"line {name}")
        for name_of_part, qty in uses[product].items():
            world.bom(product, name_of_part, qty)
    for name_of_part, qty in stock.items():
        world.stock(name_of_part, qty)
    world.add("x_workbook_row", {"record_id": "memo"}, note="Priority by memo: Alpha first, then Beta, then Gamma.")
    return Facts(world.current, world.believed, world.issues)


def random_action(rng, part=None):
    at = (NOW + timedelta(minutes=rng.randint(-60, 840))).isoformat()
    sources = [{"scenario_input": "random supply"}]
    if rng.random() < 0.2:
        return {"kind": "conditional_release", "part_id": "BOOK", "batch": "LOT",
                "quantity": rng.randint(1, 700), "available_at": at, "sources": sources}
    quantity = rng.randint(1, 2500)
    return {"kind": "offer", "quantity": rng.randint(1, quantity), "offer": {
        "offer_id": f"O{rng.randint(1, 4)}", "supplier_id": "S2", "part_id": part or rng.choice(PARTS),
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
def test_total_never_falls_with_two_editions_and_one_equally_shared_part(batch):
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


def test_one_copy_can_displace_one_of_each_later_edition():
    facts = three_editions({"P-A": 1000, "P-B": 1000, "P-C": 1000}, {"ONLY": 500, "X": 1000, "Y": 1000},
                           {"P-A": {"ONLY": 1, "X": 1, "Y": 1}, "P-B": {"X": 1}, "P-C": {"Y": 1}})
    supply = {"kind": "offer", "offer": {"offer_id": "O1", "supplier_id": "S2", "part_id": "ONLY", "quantity": 500,
                                        "available_at": NOW.isoformat(), "sources": [{"scenario_input": "supply"}]}}
    before = simulate.simulate(facts, [], now=NOW, deadline=DEADLINE)
    after = simulate.simulate(facts, [supply], now=NOW, deadline=DEADLINE)
    assert copies(before) == {"P-A": 500, "P-B": 500, "P-C": 500}
    assert copies(after) == {"P-A": 1000, "P-B": 0, "P-C": 0}


@pytest.mark.parametrize("batch", range(10))
def test_three_editions_never_fall_in_policy_order(batch):
    for seed in range(batch * 300, (batch + 1) * 300):
        rng, products = random.Random(seed), ("P-A", "P-B", "P-C")
        most = rng.choice((1, 3))                       # equal use in about half of the plans
        uses = {p: {m: rng.randint(1, most) for m in rng.sample(PARTS, rng.randint(1, 3))} for p in products}
        used = sorted({m for bom in uses.values() for m in bom})
        facts = three_editions({p: rng.randint(1, 1500) for p in products},
                               {m: rng.randint(0, 2000) for m in used}, uses)
        actions = [random_action(rng, rng.choice(used)) for _ in range(rng.randint(0, 3))]
        before = simulate.simulate(facts, actions, now=NOW, deadline=DEADLINE)
        actions.insert(rng.randint(0, len(actions)), random_action(rng, rng.choice(used)))
        after = simulate.simulate(facts, actions, now=NOW, deadline=DEADLINE)
        assert before["priority"]["order"] == list(products), seed
        assert [copies(after)[p] for p in products] >= [copies(before)[p] for p in products], seed


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
