"""Versioned source rules. One version is active; an edit creates a draft from a copy, a preview shows its effect,
activation makes it current and triggers a reconcile run."""

from __future__ import annotations

from pathlib import Path

import yaml
from psycopg.types.json import Jsonb

REQUIRED_RULE_FIELDS = ("rule_id", "key", "candidates")
REQUIRED_CANDIDATE_FIELDS = ("fact_type", "attribute")
COMPARE_MODES = ("number", "text", "date")
DECIDE_MODES = ("preference", "newest", "none")
FALLBACKS = ("lowest", "highest", "none")


class RuleError(ValueError):
    pass


def validate_rule(spec: dict) -> None:
    for name in REQUIRED_RULE_FIELDS:
        if name not in spec:
            raise RuleError(f"rule is missing {name!r}: {spec}")
    if not isinstance(spec["key"], list) or not spec["key"]:
        raise RuleError(f"{spec['rule_id']}: key must be a non-empty list")
    if not spec["candidates"]:
        raise RuleError(f"{spec['rule_id']}: needs at least one candidate")
    for cand in spec["candidates"]:
        for name in REQUIRED_CANDIDATE_FIELDS:
            if name not in cand:
                raise RuleError(f"{spec['rule_id']}: candidate is missing {name!r}: {cand}")
        if cand.get("aggregate", "latest") not in ("latest", "sum"):
            raise RuleError(f"{spec['rule_id']}: aggregate must be latest or sum")
    if spec.get("compare", "text") not in COMPARE_MODES:
        raise RuleError(f"{spec['rule_id']}: compare must be one of {COMPARE_MODES}")
    if spec.get("decide", "preference") not in DECIDE_MODES:
        raise RuleError(f"{spec['rule_id']}: decide must be one of {DECIDE_MODES}")
    if spec.get("fallback", "none") not in FALLBACKS:
        raise RuleError(f"{spec['rule_id']}: fallback must be one of {FALLBACKS}")


def active_version(conn) -> int | None:
    row = conn.execute("SELECT version FROM rule_sets WHERE active").fetchone()
    return row["version"] if row else None


def load_rule_set(conn, version: int) -> tuple[list[dict], dict]:
    rules = [r["spec"] | {"rule_id": r["rule_id"]} for r in conn.execute(
        "SELECT rule_id, spec FROM source_rules WHERE version = %s AND enabled ORDER BY position", (version,))]
    settings = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings WHERE version = %s",
                                                            (version,))}
    return rules, settings


def _insert_version(conn, rules: list[dict], settings: dict, *, created_by: str, comment: str | None,
                    parent: int | None) -> int:
    for spec in rules:
        validate_rule(spec)
    ids = [r["rule_id"] for r in rules]
    if len(ids) != len(set(ids)):
        raise RuleError("duplicate rule_id in rule set")
    version = conn.execute(
        "INSERT INTO rule_sets (parent_version, created_by, comment) VALUES (%s, %s, %s) RETURNING version",
        (parent, created_by, comment)).fetchone()["version"]
    for pos, spec in enumerate(rules):
        body = {k: v for k, v in spec.items() if k not in ("rule_id", "enabled")}
        conn.execute("INSERT INTO source_rules (version, rule_id, position, enabled, spec) VALUES (%s, %s, %s, %s, %s)",
                     (version, spec["rule_id"], pos, spec.get("enabled", True), Jsonb(body)))
    for key, value in settings.items():
        conn.execute("INSERT INTO settings (version, key, value) VALUES (%s, %s, %s)", (version, key, Jsonb(value)))
    return version


def seed(conn, seed_path: Path, created_by: str = "seed") -> int | None:
    """Create and activate version 1 from the YAML seed if no rule set exists yet."""
    if conn.execute("SELECT EXISTS (SELECT 1 FROM rule_sets) AS e").fetchone()["e"]:
        return None
    data = yaml.safe_load(seed_path.read_text())
    version = _insert_version(conn, data.get("rules", []), data.get("settings", {}), created_by=created_by,
                              comment=f"seeded from {seed_path.name}", parent=None)
    activate(conn, version)
    return version


def draft(conn, from_version: int, created_by: str, comment: str | None = None) -> int:
    rows = conn.execute("SELECT rule_id, enabled, spec FROM source_rules WHERE version = %s ORDER BY position",
                        (from_version,)).fetchall()
    if not rows and not conn.execute("SELECT 1 FROM rule_sets WHERE version = %s", (from_version,)).fetchone():
        raise RuleError(f"no rule set version {from_version}")
    rules = [r["spec"] | {"rule_id": r["rule_id"], "enabled": r["enabled"]} for r in rows]
    _, settings = load_rule_set(conn, from_version)
    return _insert_version(conn, rules, settings, created_by=created_by, comment=comment, parent=from_version)


def _require_draft(conn, version: int) -> None:
    row = conn.execute("SELECT activated_at FROM rule_sets WHERE version = %s", (version,)).fetchone()
    if row is None:
        raise RuleError(f"no rule set version {version}")
    if row["activated_at"] is not None:
        raise RuleError(f"version {version} was already activated and is immutable; create a draft from it")


def put_rule(conn, version: int, spec: dict) -> None:
    """Add or replace one rule in a draft version."""
    _require_draft(conn, version)
    validate_rule(spec)
    body = {k: v for k, v in spec.items() if k not in ("rule_id", "enabled")}
    pos = conn.execute("SELECT coalesce(max(position), -1) + 1 AS p FROM source_rules WHERE version = %s",
                       (version,)).fetchone()["p"]
    conn.execute(
        """INSERT INTO source_rules (version, rule_id, position, enabled, spec) VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (version, rule_id) DO UPDATE SET spec = EXCLUDED.spec, enabled = EXCLUDED.enabled""",
        (version, spec["rule_id"], pos, spec.get("enabled", True), Jsonb(body)))


def set_enabled(conn, version: int, rule_id: str, enabled: bool) -> None:
    _require_draft(conn, version)
    if conn.execute("UPDATE source_rules SET enabled = %s WHERE version = %s AND rule_id = %s",
                    (enabled, version, rule_id)).rowcount == 0:
        raise RuleError(f"no rule {rule_id!r} in version {version}")


def set_setting(conn, version: int, key: str, value) -> None:
    _require_draft(conn, version)
    conn.execute("INSERT INTO settings (version, key, value) VALUES (%s, %s, %s) "
                 "ON CONFLICT (version, key) DO UPDATE SET value = EXCLUDED.value", (version, key, Jsonb(value)))


def activate(conn, version: int) -> None:
    if not conn.execute("SELECT 1 FROM rule_sets WHERE version = %s", (version,)).fetchone():
        raise RuleError(f"no rule set version {version}")
    conn.execute("UPDATE rule_sets SET active = false WHERE active AND version <> %s", (version,))
    conn.execute("UPDATE rule_sets SET active = true, activated_at = coalesce(activated_at, now()) WHERE version = %s",
                 (version,))


def list_versions(conn) -> list[dict]:
    return conn.execute(
        """SELECT v.version, v.parent_version, v.active, v.created_at, v.created_by, v.comment, v.activated_at,
                  (SELECT count(*) FROM source_rules r WHERE r.version = v.version AND r.enabled) AS rules
           FROM rule_sets v ORDER BY v.version""").fetchall()
