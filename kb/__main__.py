"""CLI: python -m kb <command>."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading

import yaml

from . import consumer, db, mail_source, reconcile, rules
from .config import load_settings
from .ingest import FileReader
from . import eval_extraction, registry
from .llm.base import load_adapter
from .loader import current_sha_by_path, load_contract, load_envelopes


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def _conf_map(conn):
    return consumer._confidence_map(conn)


def cmd_migrate(args, s, conn):
    applied = db.migrate(conn, s.schema)
    print(f"schema {s.schema}: applied {applied or 'nothing (up to date)'}")


def cmd_seed_rules(args, s, conn):
    version = rules.seed(conn, s.seed_path)
    conn.commit()
    print(f"seeded and activated rule set version {version}" if version else "rule sets exist already; nothing seeded")


def cmd_load(args, s, conn):
    _print({"contract": load_contract(conn, s.structured_dir),
            "envelopes": load_envelopes(conn, s.structured_dir, s.data_dir, _conf_map(conn))})


def cmd_sync(args, s, conn):
    load_contract(conn, s.structured_dir)
    _print(consumer.sync(conn, s, FileReader(s.data_dir), _conf_map(conn), force=args.force))


def cmd_reconcile(args, s, conn):
    result = reconcile.run(conn, "manual", {"by": args.by})
    _print({k: v for k, v in result.items() if k != "changes"} |
           {"changes": {k: len(v) for k, v in result["changes"].items()}})


def _adapter(spec):
    client = load_adapter(spec)
    if client is None:
        raise SystemExit("no LLM adapter configured (KB_LLM_ADAPTER=package.module:ClassName or --adapter)")
    return client


def cmd_extract(args, s, conn):
    client = _adapter(args.adapter or s.llm_adapter)
    drained, parked = consumer.extract(conn, s, FileReader(s.data_dir), client, mail_source.MailSource.from_env(),
                                       _conf_map(conn), limit=args.limit)
    _print({**drained, "mail_parked": len(parked)})
    result = reconcile.run(conn, "llm_extraction", {"adapter": client.label})
    _print({"changes": {k: len(v) for k, v in result["changes"].items()}})


def cmd_eval_extraction(args, s, conn):
    client = _adapter(args.adapter or s.llm_adapter)
    result = eval_extraction.run(conn, client, s.structured_dir, FileReader(s.data_dir),
                                 current_sha_by_path(conn, s.data_dir), kinds=args.kind or None, limit=args.limit)
    for kind, b in sorted(result.items()):
        print(f"{kind:28} files={b['files']:<4} reference facts={b['ref_facts']:<6} matched={b['matched']:<6} "
              f"agree={b['agree']:<6} agreement={b['agreement']}  extra={b['extra_facts']}  errors={len(b['errors'])}")
    if args.full:
        _print(result)


def cmd_profiles(args, s, conn):
    rows = conn.execute("SELECT supplier_id, profile, computed_at FROM supplier_profiles "
                        "WHERE %s::text IS NULL OR supplier_id = %s ORDER BY supplier_id",
                        (args.supplier, args.supplier)).fetchall()
    if args.supplier or args.full:
        _print(rows)
        return
    for r in rows:
        p, d = r["profile"], r["profile"]["delivery"]
        name = (p.get("master") or {}).get("name", "")
        print(f"{r['supplier_id']:9} {name[:28]:28} late {d['late_lines']}/{d['delivered_lines']} "
              f"({d['late_share']}) avg {d['avg_delay_days_when_late']} d | open lines {d['open_lines']} "
              f"{d['open_value']} | open issues {p['issues']['open']} {p['issues']['by_severity']}")


def cmd_review(args, s, conn):
    _print(registry.pending_review(conn))


def cmd_promote(args, s, conn):
    registry.promote(conn, args.object_kind.replace("-", "_"), args.name, args.by, alias_of=args.alias,
                     reject=args.cmd == "reject")
    conn.commit()
    result = reconcile.run(conn, "promotion", {"by": args.by, "object": args.object_kind, "name": args.name})
    _print({"done": f"{args.cmd} {args.object_kind} {args.name}", "run_id": result["run_id"],
            "changes": {k: len(v) for k, v in result["changes"].items()}})


def cmd_run(args, s, conn):
    conn.close()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    consumer.run(s, stop)


def cmd_status(args, s, conn):
    out = {
        "rule_set_version": rules.active_version(conn),
        "documents": {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, count(*) AS n FROM documents WHERE active GROUP BY status")},
        "facts_valid": conn.execute("SELECT count(*) AS n FROM facts WHERE valid").fetchone()["n"],
        "fact_types": {r["fact_type"]: r["n"] for r in conn.execute(
            "SELECT fact_type, count(*) AS n FROM facts WHERE valid GROUP BY fact_type ORDER BY 2 DESC")},
        "queue": {r["state"]: r["n"] for r in conn.execute(
            "SELECT state, count(*) AS n FROM extraction_queue GROUP BY state")},
        "open_issues": {f"{r['kind']}/{r['severity']}": r["n"] for r in conn.execute(
            "SELECT kind, severity, count(*) AS n FROM issues WHERE state = 'open' GROUP BY 1, 2 ORDER BY 1, 2")},
        "last_run": conn.execute("SELECT run_id, trigger, finished_at, n_issues, n_new, n_changed, n_resolved "
                                 "FROM runs WHERE NOT preview ORDER BY run_id DESC LIMIT 1").fetchone(),
    }
    _print(out)


def cmd_issues(args, s, conn):
    rows = conn.execute(
        "SELECT issue_key, kind, severity, title FROM issues WHERE state = %s AND (%s::text IS NULL OR kind = %s) "
        "ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'cost' THEN 1 WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END,"
        " kind, title LIMIT %s", (args.state, args.kind, args.kind, args.limit)).fetchall()
    for r in rows:
        print(f"{r['severity']:6} {r['kind']:22} {r['issue_key']}  {r['title']}")


def cmd_explain(args, s, conn):
    row = conn.execute("SELECT * FROM issues WHERE issue_key = %s", (args.issue_key,)).fetchone()
    if not row:
        raise SystemExit(f"no issue {args.issue_key}")
    _print(row)


def cmd_rules(args, s, conn):
    if args.action == "list":
        for v in rules.list_versions(conn):
            mark = "*" if v["active"] else " "
            print(f"{mark} v{v['version']:<3} parent={v['parent_version']} rules={v['rules']} by={v['created_by']} "
                  f"activated={v['activated_at'] or '-'}  {v['comment'] or ''}")
        return
    if args.action == "show":
        version = args.version or rules.active_version(conn)
        rule_list, settings = rules.load_rule_set(conn, version)
        print(yaml.safe_dump({"version": version, "rules": rule_list, "settings": settings}, sort_keys=False,
                             allow_unicode=True))
        return
    if args.action == "draft":
        version = rules.draft(conn, args.version or rules.active_version(conn), args.by, args.comment)
        conn.commit()
        print(f"created draft version {version}")
        return
    if args.action == "put":
        spec = yaml.safe_load(sys.stdin.read() if args.file == "-" else open(args.file).read())
        for item in spec if isinstance(spec, list) else [spec]:
            rules.put_rule(conn, args.version, item)
        conn.commit()
        print(f"updated draft version {args.version}")
        return
    if args.action in ("enable", "disable"):
        rules.set_enabled(conn, args.version, args.rule_id, args.action == "enable")
        conn.commit()
        print(f"{args.action}d {args.rule_id} in version {args.version}")
        return
    if args.action == "set":
        rules.set_setting(conn, args.version, args.key, json.loads(args.value))
        conn.commit()
        print(f"set {args.key} in version {args.version}")
        return
    if args.action == "preview":
        result = reconcile.run(conn, "rule_preview", {"by": args.by}, version=args.version, preview=True)
        conn.rollback()
        _print({"rule_set_version": args.version, "issues": result["issues"], "changes": result["changes"]})
        return
    if args.action == "activate":
        rules.activate(conn, args.version)
        conn.commit()
        result = reconcile.run(conn, "rule_change", {"by": args.by, "version": args.version})
        _print({"activated": args.version, "run_id": result["run_id"],
                "changes": {k: len(v) for k, v in result["changes"].items()}})
        return


def cmd_check_structured(args) -> int:
    import os
    from pathlib import Path
    from tracker.config import PROJECT_ROOT
    from .check_structured import run
    structured = Path(os.environ.get("KB_STRUCTURED_DIR", PROJECT_ROOT / "structured"))
    current = Path(os.environ.get("TRACKER_DATA_DIR", PROJECT_ROOT / "data")) / "current"
    result = run(structured, current)
    if not args.full:
        m, e = result["maps"], result["envelopes"]
        result = {"ok": result["ok"], "failures": result["failures"],
                  "maps": {"valid": m["maps_valid"], "problems": m["map_problems"][:10], "tables": m["tables"],
                           "unmatched_tables": m["unmatched_tables"],
                           "with_parse_errors": [p for p in m["parsed"] if p["parse_errors"] or p["row_id_subjects"]]},
                  "envelopes": {k: v for k, v in e.items() if k != "attribute_coverage"},
                  "low_attribute_coverage": {ft: {a: c for a, c in v["attributes"].items() if c < 0.5}
                                             for ft, v in e["attribute_coverage"].items()
                                             if any(c < 0.5 for c in v["attributes"].values())}}
    _print(result)
    return 0 if result["ok"] else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m kb")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="create or update the kb schema").set_defaults(func=cmd_migrate)
    sub.add_parser("seed-rules", help="create rule set version 1 from rules.seed.yaml").set_defaults(func=cmd_seed_rules)
    sub.add_parser("load", help="load structured/ (contract, schema maps, envelopes)").set_defaults(func=cmd_load)
    p = sub.add_parser("sync", help="ingest the tracker's current files that kb has not processed yet")
    p.add_argument("--force", action="store_true", help="re-process every current file")
    p.set_defaults(func=cmd_sync)
    p = sub.add_parser("reconcile", help="evaluate rules and update issues")
    p.add_argument("--by", default="cli")
    p.set_defaults(func=cmd_reconcile)
    p = sub.add_parser("extract", help="run an LLM adapter over the extraction queue")
    p.add_argument("--adapter", help="package.module:ClassName (default KB_LLM_ADAPTER)")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_extract)
    p = sub.add_parser("eval-extraction", help="score an LLM adapter against the reference envelopes")
    p.add_argument("--adapter", help="package.module:ClassName (default KB_LLM_ADAPTER)")
    p.add_argument("--kind", action="append", help="doc kind(s) to score; default all")
    p.add_argument("--limit", type=int, default=20, help="files per doc kind")
    p.add_argument("--full", action="store_true")
    p.set_defaults(func=cmd_eval_extraction)
    p = sub.add_parser("profiles", help="supplier profiles (rebuilt on every reconcile)")
    p.add_argument("supplier", nargs="?")
    p.add_argument("--full", action="store_true")
    p.set_defaults(func=cmd_profiles)
    sub.add_parser("review", help="what agents proposed and what failed the validation gate").set_defaults(
        func=cmd_review)
    for name in ("promote", "reject"):
        p = sub.add_parser(name, help=f"{name} a proposed doc kind, schema map or fact type")
        p.add_argument("object_kind", choices=["doc-kind", "schema-map", "fact-type"])
        p.add_argument("name")
        p.add_argument("--alias", help="promote a proposed fact type as an alias of this core type")
        p.add_argument("--by", required=True, help="who approves")
        p.set_defaults(func=cmd_promote)
    sub.add_parser("run", help="follow the tracker's change feed").set_defaults(func=cmd_run)
    p = sub.add_parser("check-structured", help="acceptance check for structured/ (no database needed)")
    p.add_argument("--full", action="store_true", help="print every detail, not just the summary")
    p.set_defaults(func=None)
    sub.add_parser("status").set_defaults(func=cmd_status)
    p = sub.add_parser("issues")
    p.add_argument("--state", default="open", choices=["open", "resolved"])
    p.add_argument("--kind")
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(func=cmd_issues)
    p = sub.add_parser("explain")
    p.add_argument("issue_key")
    p.set_defaults(func=cmd_explain)
    p = sub.add_parser("rules", help="versioned source rules")
    p.add_argument("action", choices=["list", "show", "draft", "put", "enable", "disable", "set", "preview", "activate"])
    p.add_argument("--version", type=int)
    p.add_argument("--rule-id")
    p.add_argument("--key")
    p.add_argument("--value", help="JSON value for `set`")
    p.add_argument("--file", default="-", help="YAML rule (or list of rules) for `put`; - reads stdin")
    p.add_argument("--by", default="cli")
    p.add_argument("--comment")
    p.set_defaults(func=cmd_rules)
    args = parser.parse_args(argv)
    if args.cmd == "rules" and args.action in ("put", "enable", "disable", "set", "preview", "activate") \
            and not args.version:
        parser.error(f"rules {args.action} needs --version")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.cmd == "check-structured":
        return cmd_check_structured(args)
    s = load_settings()
    conn = db.connect(s.database_url, s.schema)
    try:
        args.func(args, s, conn)
    finally:
        if not conn.closed:
            conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
