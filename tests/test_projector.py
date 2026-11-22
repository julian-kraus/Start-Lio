"""The record projector on synthetic facts: supplier records, the plan record, sources and idempotency."""

import json
import os

import pytest

from sourcing import reliability, weakspots
from sourcing.facts import Facts
from sourcing_world import NOW, DEADLINE, World, part
from tracker import lookahead

import projector
from projector import launch, risk, suppliers

# Every number a projected record writes must sit inside an object that carries its source: a figure with
# fact ids, a score or spot with sources, or a goal line / scenario input. These are the source keys.
SOURCE_KEYS = frozenset({"fact_ids", "sources", "source", "goal_line", "scenario_input", "goal_lines"})


# ---- synthetic promise history (raw kb.current_facts rows) -------------------------------------------------

def facts_rows(fact_type, subject, doc="d", **attrs):
    tag = "-".join(str(v) for v in subject.values())
    return [{"fact_id": f"{doc}:{fact_type}:{tag}:{name}", "fact_type": fact_type, "subject": subject,
             "attribute": name, "value": value, "as_of": None, "path": doc} for name, value in attrs.items()]


def po_line(po, sup="S1", actual="2030-01-10", promised="2030-01-10", qty=100, part_id="P1", line=1):
    return facts_rows("po_line", {"po_number": po, "po_line": line}, supplier_id=sup, promised_date=promised,
                      actual_delivery_date=actual, qty=qty, material_id=part_id)


def orders(sup, kept, broken):
    return [row for i in range(kept + broken)
            for row in po_line(f"{sup}-{i}", sup=sup, actual="2030-01-10" if i < kept else "2030-01-12")]


def read_log(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def violations(node, sourced=False):
    """Every number that no ancestor object gives a source for."""
    if isinstance(node, dict):
        here = sourced or any(k in SOURCE_KEYS for k in node)
        for value in node.values():
            yield from violations(value, here)
    elif isinstance(node, list):
        for value in node:
            yield from violations(value, sourced)
    elif isinstance(node, bool):
        return
    elif isinstance(node, (int, float)):
        if not sourced:
            yield node


# ---- supplier records --------------------------------------------------------------------------------------

def test_supplier_records_equal_reliability_output():
    rows = orders("S1", 6, 2) + orders("S2", 2, 6)
    facts = Facts(rows)
    got = suppliers.records(list(facts.by_id.values()), facts.believed("received_qty"))
    want = reliability.cards(reliability.history(rows))
    assert got == want
    assert {r["supplier_id"] for r in got} == {"S1", "S2"}
    assert all(r["kind"] == "supplier" for r in got)


def test_supplier_score_carries_its_counts():
    facts = Facts(orders("S1", 6, 2))
    (card,) = suppliers.records(list(facts.by_id.values()), facts.believed("received_qty"))
    assert card["score"]["history"] == {"kept": 6, "n": 8}


# ---- the plan record ---------------------------------------------------------------------------------------

def goal():
    return {"statement": "every edition packed", "unit_loss_eur": 120}


def world_facts(world):
    return Facts(world.current, world.believed, world.issues)


def test_plan_record_has_one_lane_per_part_with_coverage_figures():
    world = World()
    coverage = world.run()
    record = risk.plan_record(coverage, [])
    assert record["kind"] == "plan"
    assert {lane["lane_id"] for lane in record["lanes"]} == {p["material_id"] for p in coverage["parts"]}
    assert record["coverage"]["covered"] == coverage["covered"]
    assert record["coverage"]["needed"] == coverage["needed"]
    assert record["lanes"][0]["need"] == part(coverage, record["lanes"][0]["lane_id"])["need"]


def test_plan_record_folds_in_the_weak_spot_status():
    world = World()
    coverage = world.run()
    spots = weakspots.rank(world_facts(world), coverage, goal())
    record = risk.plan_record(coverage, spots)
    ranked = {lane["lane_id"]: lane for lane in record["lanes"] if "status" in lane}
    assert ranked, "expected at least one lane to carry a weak-spot status"
    worst = spots[0]
    assert ranked[worst["part_id"]]["status"] == worst["detail"]["status"]
    assert ranked[worst["part_id"]]["reason"] == worst["reason"]


# ---- the source invariant ----------------------------------------------------------------------------------

def test_every_number_in_a_projected_record_carries_a_source():
    world = World()
    coverage = world.run()
    spots = weakspots.rank(world_facts(world), coverage, goal())
    rows = orders("S1", 6, 2) + orders("S2", 2, 6)
    records = suppliers.records(rows, []) + [risk.plan_record(coverage, spots)]
    for record in records:
        bare = list(violations(record))
        assert not bare, f"{record['kind']} record has unsourced numbers: {bare}"


# ---- determinism and idempotency -------------------------------------------------------------------------

def test_content_key_ignores_the_producer_stamp_and_the_log_position():
    record = {"kind": "supplier", "supplier_id": "S1", "score": {"value": 0.5}}
    assert launch.content_key(record) == launch.content_key({**record, "producer": "projector", "id": 7})
    assert launch.content_key(record) != launch.content_key({**record, "supplier_id": "S2"})


def test_projecting_the_same_state_twice_writes_nothing_the_second_time(tmp_path):
    log = tmp_path / "lookahead.log"
    facts = Facts(orders("S1", 6, 2) + orders("S2", 2, 6))
    records = launch.project(list(facts.by_id.values()), facts, None, now=NOW, deadline=None)
    first = launch.write_new(log, records)
    assert len(first) == len(records) > 0
    assert launch.write_new(log, records) == []
    assert len(read_log(log)) == len(first)


def test_a_changed_fact_supersedes_the_old_supplier_record(tmp_path):
    log = tmp_path / "lookahead.log"
    before = Facts(orders("S1", 6, 2))
    launch.write_new(log, launch.project(list(before.by_id.values()), before, None, now=NOW, deadline=None))
    after = Facts(orders("S1", 2, 6))      # S1's promises are now mostly broken: a lower score
    fresh = launch.write_new(log, launch.project(list(after.by_id.values()), after, None, now=NOW, deadline=None))
    assert len(fresh) == 1 and fresh[0]["supplier_id"] == "S1"
    folded = lookahead.fold([{**r, "id": n} for n, r in enumerate(read_log(log), 1)])
    assert folded["suppliers"]["S1"]["score"]["value"] == fresh[0]["score"]["value"]


def test_projected_records_validate_as_lookahead_records(tmp_path):
    facts = Facts(orders("S1", 6, 2))
    world = World()
    coverage = world.run()
    records = suppliers.records(list(facts.by_id.values()), []) + [risk.plan_record(coverage, [])]
    for record in records:
        lookahead.validate(record)


# ---- the run() path against a real kb ----------------------------------------------------------------------

def _seed(conn, schema, rows):
    from psycopg import sql
    from psycopg.types.json import Jsonb
    with conn.cursor() as cur:
        cur.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema)))
        for row in rows:
            cur.execute(
                "INSERT INTO facts (fact_id, fact_type, subject, attribute, value, extractor, confidence, "
                "source_path, source_sha256, valid) VALUES (%s,%s,%s,%s,%s,'test',1.0,%s,'t',true) "
                "ON CONFLICT (fact_id) DO NOTHING",
                (row["fact_id"], row["fact_type"], Jsonb(row["subject"]), row["attribute"],
                 Jsonb(row["value"]), row["path"]))
    conn.commit()


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="no throwaway Postgres")
def test_run_once_projects_supplier_records_from_a_real_kb(tmp_path, monkeypatch):
    from kb import db as kb_db
    url = os.environ["TEST_DATABASE_URL"]
    schema = "kb_projector_test"
    monkeypatch.setenv("KB_SCHEMA", schema)
    conn = kb_db.connect(url, schema)
    try:
        conn.execute("DROP SCHEMA IF EXISTS kb_projector_test CASCADE")
        conn.commit()
        kb_db.migrate(conn, schema)
        _seed(conn, schema, orders("S1", 6, 2) + orders("S2", 2, 6))
    finally:
        conn.close()

    log = tmp_path / "lookahead.log"
    fresh = launch.run(log, url, once=True, schema=schema)
    assert {r["supplier_id"] for r in fresh} == {"S1", "S2"}
    assert all(r["kind"] == "supplier" for r in fresh)
    written = read_log(log)
    assert written and all(r["producer"] == "projector" for r in written)
    assert launch.run(log, url, once=True, schema=schema) == []       # idempotent second run
