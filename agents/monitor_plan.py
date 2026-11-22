"""The plan record of the launch edition, once the goal is set: coverage from kb, nothing typed in."""
from __future__ import annotations

from plan import clock
from sourcing import coverage
from tracker.lookahead import append_records


def record(known, rows: list[dict], product: str, now, deadline) -> dict:
    solved = coverage.compute(known, now=now, deadline=deadline)
    edition = next(e for e in solved["editions"] if e["product_id"] == product)
    names = {r["subject"].get("material_id"): r["value"] for r in rows
             if r["fact_type"] == "component" and r["attribute"] == "description"}
    lanes = []
    for part in solved["parts"]:
        for share in part["allocation"]:
            if share["product_id"] != product:
                continue
            need, got = share["need"]["value"], share["allocated"]["value"]
            lanes.append({"lane_id": part["material_id"], "name": names.get(part["material_id"], part["material_id"]),
                          "edition": edition["name"], "need": need, "usable": got, "gap": max(0, need - got),
                          "status": "critical" if need > got else "ok", "bars": [],
                          "sources": [{"fact_id": f} for f in share["allocated"]["fact_ids"]]})
    lanes.sort(key=lambda lane: -lane["gap"])
    return {"kind": "plan", "at": now.isoformat(), "lanes": lanes, "coverage": {
        "needed": edition["demand"]["value"], "covered": edition["packed"]["value"] + edition["covered"]["value"],
        "unit": "copies", "sources": [{"fact_id": f} for f in edition["covered"]["fact_ids"]]}}


def publish(monitor, state: dict, producer: str) -> None:
    launch = state["launch"] or {}
    if not launch.get("edition") or not state["goal"] or state.get("plan"):
        return
    known = monitor.load_facts(monitor.kb, monitor.kb_schema)
    rows = monitor.kb.execute("SELECT * FROM current_facts").fetchall()
    monitor.kb.commit()
    now = clock.now(monitor.plan)
    monitor.plan.commit()
    append_records(monitor.log, [record(known, rows, launch["edition"], now, clock.parse(launch["deadline"]))],
                   producer=producer)
