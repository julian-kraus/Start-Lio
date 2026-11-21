"""The structure registry: doc kinds (structure files), schema maps and fact types, with their lifecycle status.

core     from the contract (Codex's first pass)
auto     a table schema map proposed by the designer that passed the parse checks; used immediately
proposed created by an agent; its facts are kept but count with capped confidence until promoted
approved promoted by a person
rejected not used
"""

from __future__ import annotations

import re

import yaml
from psycopg.types.json import Jsonb

from tracker.db import _jsonb_safe

from .llm.base import Structure

ACTIVE_MAP_STATUS = ("core", "auto", "approved")
USABLE_KIND_STATUS = ("core", "approved", "proposed")
_FENCED = re.compile(r"```(?:ya?ml)?\s*\n(.*?)```", re.DOTALL)
_FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def parse_recognise(markdown: str) -> dict | None:
    """The machine-readable `recognise:` block of a doc-kind file (YAML front matter or a fenced yaml block)."""
    for block in [m.group(1) for m in _FRONT.finditer(markdown)] + [m.group(1) for m in _FENCED.finditer(markdown)]:
        try:
            data = yaml.safe_load(block)
        except yaml.YAMLError:
            continue
        if isinstance(data, dict) and isinstance(data.get("recognise"), dict):
            rec = data["recognise"]
            if isinstance(data.get("checks"), list):
                rec = {**rec, "checks": data["checks"]}
            return rec
    return None


def upsert_doc_kind(conn, name: str, description: str, *, origin: str, status: str, created_by: str | None = None,
                    recognise: dict | None = None) -> None:
    recognise = recognise if recognise is not None else parse_recognise(description)
    conn.execute(
        """INSERT INTO doc_kinds (name, description, origin, status, recognise, created_by)
           VALUES (%s, %s, %s, %s, %s, %s)
           ON CONFLICT (name) DO UPDATE SET description = EXCLUDED.description, recognise = EXCLUDED.recognise,
             origin = EXCLUDED.origin, version = doc_kinds.version + 1, updated_at = now(),
             status = CASE WHEN doc_kinds.status IN ('approved', 'rejected') THEN doc_kinds.status
                           ELSE EXCLUDED.status END""",
        (name, description, origin, status, Jsonb(_jsonb_safe(recognise)) if recognise else None, created_by))


def upsert_schema_map(conn, mapping: dict, *, origin: str, status: str, created_by: str | None = None,
                      validation: dict | None = None) -> None:
    conn.execute(
        """INSERT INTO schema_maps (signature, format, sheet, mapping, origin, status, created_by, validation)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (signature) DO UPDATE SET format = EXCLUDED.format, sheet = EXCLUDED.sheet,
             mapping = EXCLUDED.mapping, origin = EXCLUDED.origin, validation = EXCLUDED.validation,
             updated_at = now(),
             status = CASE WHEN schema_maps.status IN ('approved', 'rejected') THEN schema_maps.status
                           ELSE EXCLUDED.status END""",
        (mapping["signature"], mapping.get("format"), mapping.get("sheet"), Jsonb(_jsonb_safe(mapping)), origin,
         status, created_by, Jsonb(_jsonb_safe(validation)) if validation else None))


def structures(conn) -> list[Structure]:
    return [Structure(r["name"], r["description"], r["recognise"], r["status"]) for r in conn.execute(
        "SELECT name, description, recognise, status FROM doc_kinds WHERE status = ANY(%s) ORDER BY name",
        (list(USABLE_KIND_STATUS),))]


def structure(conn, name: str) -> Structure | None:
    r = conn.execute("SELECT name, description, recognise, status FROM doc_kinds WHERE name = %s", (name,)).fetchone()
    return Structure(r["name"], r["description"], r["recognise"], r["status"]) if r else None


def known_fact_types(conn) -> set[str]:
    return {r["name"] for r in conn.execute("SELECT name FROM fact_types WHERE status <> 'rejected'")}


def contract_summary(conn) -> dict:
    """What an adapter needs to know about the vocabulary (core and approved types first)."""
    return {"fact_types": [
        {"name": r["name"], "description": r["description"], "attributes": r["attributes"], "status": r["status"]}
        for r in conn.execute("SELECT name, description, attributes, status FROM fact_types "
                              "WHERE status <> 'rejected' ORDER BY status = 'proposed', name")]}


def promote(conn, object_kind: str, name: str, by: str, *, alias_of: str | None = None, reject: bool = False) -> None:
    table = {"doc_kind": ("doc_kinds", "name"), "schema_map": ("schema_maps", "signature"),
             "fact_type": ("fact_types", "name")}[object_kind]
    status = "rejected" if reject else "approved"
    if alias_of and object_kind != "fact_type":
        raise ValueError("only fact types can be aliased")
    if alias_of and not conn.execute("SELECT 1 FROM fact_types WHERE name = %s AND status IN ('core', 'approved')",
                                     (alias_of,)).fetchone():
        raise ValueError(f"{alias_of!r} is not a core or approved fact type")
    sql_ = f"UPDATE {table[0]} SET status = %s" + (", alias_of = %s" if alias_of else "") + f" WHERE {table[1]} = %s"
    params = (status, alias_of, name) if alias_of else (status, name)
    if conn.execute(sql_, params).rowcount == 0:
        raise ValueError(f"no {object_kind} {name!r}")
    conn.execute("INSERT INTO promotions (by_user, object_kind, name, action, detail) VALUES (%s, %s, %s, %s, %s)",
                 (by, object_kind, name, "reject" if reject else ("alias" if alias_of else "promote"),
                  Jsonb({"alias_of": alias_of} if alias_of else {})))


def pending_review(conn) -> dict:
    return {
        "doc_kinds": conn.execute("SELECT name, created_by, updated_at, recognise FROM doc_kinds "
                                  "WHERE status = 'proposed' ORDER BY updated_at").fetchall(),
        "schema_maps": conn.execute("SELECT signature, format, sheet, mapping->>'fact_type' AS fact_type, "
                                    "created_by, validation FROM schema_maps WHERE status = 'proposed'").fetchall(),
        "fact_types": conn.execute("SELECT name, description, origin FROM fact_types WHERE status = 'proposed' "
                                   "ORDER BY name").fetchall(),
        "documents": conn.execute("SELECT path, sha256, doc_kind, detail->'gate' AS gate FROM documents "
                                  "WHERE active AND status = 'needs_review'").fetchall(),
    }
