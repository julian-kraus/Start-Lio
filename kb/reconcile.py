"""One reconcile run: active rule set + valid facts -> believed values + issues, diffed against the previous run."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from tracker.db import _jsonb_safe

from . import db, rules as rules_mod
from .engine import Issue, build_records, evaluate, issue_key
from .facts import load_valid_facts


def _now(settings: dict) -> datetime:
    value = settings.get("evaluation_time")
    if value:
        ts = datetime.fromisoformat(value)
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def state_issues(conn) -> list[Issue]:
    """Issues that come from the state of ingestion rather than from rules."""
    issues = []
    for d in conn.execute("SELECT path, sha256, container, status, detail FROM documents "
                          "WHERE active AND status IN ('needs_schema_map', 'partial', 'failed')"):
        kind = "ingest_failed" if d["status"] == "failed" else "schema_map_needed"
        detail = d["detail"] or {}
        what = (f"cannot read {d['path']}: {detail.get('error')}" if kind == "ingest_failed" else
                f"new table layout in {d['path']}: no schema map for "
                f"{', '.join(u.get('sheet') or 'the table' for u in detail.get('unmatched', [])) or 'its header'}")
        issues.append(Issue(issue_key(kind, d["path"], d["sha256"]), kind, "medium", what,
                            {"path": d["path"], "sha256": d["sha256"], "container": d["container"], **detail}, []))
    for q in conn.execute("SELECT path, sha256, container, state, attempts, last_error FROM extraction_queue "
                          "WHERE state IN ('pending', 'failed')"):
        kind = "pending_extraction" if q["state"] == "pending" else "extraction_failed"
        issues.append(Issue(issue_key(kind, q["path"], q["sha256"]), kind, "medium",
                            f"{q['path']} is not read yet" if kind == "pending_extraction"
                            else f"{q['path']} could not be extracted: {q['last_error']}",
                            dict(q), []))
    if db.tracker_table_exists(conn, "stale_flags"):
        for s in conn.execute("SELECT path, flagged_at FROM public.stale_flags"):
            issues.append(Issue(issue_key("stale_source", s["path"], ""), "stale_source", "high",
                                f"{s['path']} has stopped changing (stale since {s['flagged_at']:%Y-%m-%d %H:%M} UTC)",
                                {"path": s["path"], "flagged_at": s["flagged_at"].isoformat()}, []))
    registry = {r["name"] for r in conn.execute("SELECT name FROM fact_types")}
    for t in conn.execute("SELECT fact_type, count(*) AS n, min(source_path) AS example FROM facts "
                          "WHERE valid GROUP BY fact_type"):
        unknown = t["fact_type"].startswith("x_") or (registry and t["fact_type"] not in registry)
        if unknown:
            issues.append(Issue(issue_key("new_information_type", t["fact_type"], ""), "new_information_type", "low",
                                f"new kind of information '{t['fact_type']}' ({t['n']} facts, e.g. {t['example']}); "
                                f"no rule uses it yet", dict(t), []))
    return issues


def _hash(issue: Issue) -> str:
    body = json.dumps([issue.severity, issue.title, issue.payload], sort_keys=True, default=str)
    return hashlib.sha256(body.encode()).hexdigest()


def run(conn, trigger: str, detail: dict | None = None, version: int | None = None, preview: bool = False) -> dict:
    """Evaluate and persist. With preview=True everything happens inside a savepoint that is rolled back, so the
    caller gets the diff a rule set would cause without changing anything."""
    version = version or rules_mod.active_version(conn)
    if version is None:
        raise SystemExit("no active rule set; run `python -m kb seed-rules` first")
    rule_list, settings = rules_mod.load_rule_set(conn, version)
    now = _now(settings)
    records = build_records(load_valid_facts(conn))
    believed, rule_issues, diagnostics = evaluate(rule_list, records, settings, now)
    issues = {i.issue_key: i for i in rule_issues + state_issues(conn)}

    tx = conn.transaction(force_rollback=preview)
    with tx:
        run_id = conn.execute(
            "INSERT INTO runs (trigger, detail, rule_set_version, preview) VALUES (%s, %s, %s, %s) RETURNING run_id",
            (trigger, Jsonb(_jsonb_safe({**(detail or {}), "diagnostics": diagnostics})), version, preview),
        ).fetchone()["run_id"]
        conn.execute("DELETE FROM believed")
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO believed (rule_id, key, rule_set_version, value, winner_label, winning_fact_ids,
                                         losing_fact_ids, contradiction, explanation, run_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                [(b.rule_id, Jsonb(b.key), version, Jsonb(_jsonb_safe(b.value)), b.winner_label,
                  b.winning_fact_ids, b.losing_fact_ids, b.contradiction, b.explanation, run_id) for b in believed])

        existing = {r["issue_key"]: r for r in conn.execute("SELECT issue_key, state, payload_hash FROM issues")}
        changes = {"new": [], "changed": [], "resolved": [], "reopened": []}
        for key, issue in issues.items():
            h = _hash(issue)
            old = existing.get(key)
            if old is None:
                change = "new"
            elif old["state"] == "resolved":
                change = "reopened"
            elif old["payload_hash"] != h:
                change = "changed"
            else:
                continue
            conn.execute(
                """INSERT INTO issues (issue_key, kind, severity, state, title, payload, payload_hash, fact_ids,
                                       rule_set_version, run_id)
                   VALUES (%s, %s, %s, 'open', %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (issue_key) DO UPDATE SET severity = EXCLUDED.severity, state = 'open',
                     title = EXCLUDED.title, payload = EXCLUDED.payload, payload_hash = EXCLUDED.payload_hash,
                     fact_ids = EXCLUDED.fact_ids, rule_set_version = EXCLUDED.rule_set_version,
                     run_id = EXCLUDED.run_id, last_changed = now(), resolved_at = NULL""",
                (key, issue.kind, issue.severity, issue.title, Jsonb(_jsonb_safe(issue.payload)), h, issue.fact_ids,
                 version, run_id))
            changes[change].append(key)
        for key, old in existing.items():
            if old["state"] == "open" and key not in issues:
                conn.execute("UPDATE issues SET state = 'resolved', resolved_at = now(), last_changed = now(), "
                             "run_id = %s WHERE issue_key = %s", (run_id, key))
                changes["resolved"].append(key)
        for change, keys in changes.items():
            for key in keys:
                conn.execute("INSERT INTO issue_events (run_id, issue_key, change, payload) VALUES (%s, %s, %s, %s)",
                             (run_id, key, change, Jsonb(_jsonb_safe(
                                 {"title": issues[key].title, "kind": issues[key].kind,
                                  "severity": issues[key].severity} if key in issues else None))))
        n_changed = len(changes["changed"]) + len(changes["reopened"])
        conn.execute("UPDATE runs SET finished_at = now(), n_issues = %s, n_new = %s, n_changed = %s, n_resolved = %s "
                     "WHERE run_id = %s", (len(issues), len(changes["new"]), n_changed, len(changes["resolved"]), run_id))
        if not preview and any(changes.values()):
            conn.execute("SELECT pg_notify(%s, %s)", (db.ISSUE_CHANNEL, json.dumps(
                {"run_id": run_id, **{k: len(v) for k, v in changes.items()}})))
    if not preview:
        conn.commit()
    return {"run_id": run_id, "rule_set_version": version, "issues": len(issues), "believed": len(believed),
            "diagnostics": diagnostics, "preview": preview,
            "changes": {k: [{"issue_key": key, "title": issues[key].title if key in issues else None} for key in v]
                        for k, v in changes.items()}}
