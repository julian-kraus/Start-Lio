"""Fact change events for downstream layers: one row per document whose valid facts changed, plus NOTIFY kb_facts."""

from __future__ import annotations

import json

FACT_CHANNEL = "kb_facts"


def _snapshot(conn, path: str) -> tuple[str | None, int, list[str]]:
    rows = conn.execute(
        "SELECT source_sha256, fact_type, count(*) AS n FROM facts WHERE source_path = %s AND valid "
        "GROUP BY 1, 2", (path,)).fetchall()
    shas = {r["source_sha256"] for r in rows}
    return (next(iter(shas)) if len(shas) == 1 else None), sum(r["n"] for r in rows), sorted({r["fact_type"] for r in rows})


def snapshot(conn, path: str) -> dict:
    """Call before changing a document's facts; pass the result to emit() afterwards."""
    sha, n, types = _snapshot(conn, path)
    return {"sha": sha, "n": n, "types": types, "fact_ids": _fact_ids(conn, path)}


def _fact_ids(conn, path: str) -> set[str]:
    return {r["fact_id"] for r in conn.execute("SELECT fact_id FROM facts WHERE source_path = %s AND valid", (path,))}


def emit(conn, path: str, before: dict, *, sha: str | None, status: str | None) -> int | None:
    """Write an event if the set of valid facts for this path changed. Returns the event id or None."""
    after_ids = _fact_ids(conn, path)
    if after_ids == before["fact_ids"]:
        return None
    _, n, types = _snapshot(conn, path)
    if not after_ids:
        change = "facts_removed" if before["fact_ids"] else "no_facts"
    elif before["fact_ids"]:
        change = "facts_replaced"
    else:
        change = "facts_added"
    previous = [before["sha"]] if before["sha"] and before["sha"] != sha else []
    row = conn.execute(
        """INSERT INTO fact_events (path, sha256, previous_sha256, change, status, n_facts, fact_types)
           VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (path, sha, previous, change, status, n, types)).fetchone()
    conn.execute("SELECT pg_notify(%s, %s)", (FACT_CHANNEL, json.dumps(
        {"id": row["id"], "path": path, "sha256": sha, "change": change, "n_facts": n})))
    return row["id"]
