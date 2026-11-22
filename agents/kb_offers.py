"""Copy supplier offers read from documents (kb `offer` facts) into plan.offers, once per offer.

An offer arrives as one kb fact per attribute under the subject (supplier_id, offer_ref, item). The monitor's
re-plan reads only plan.offers, so without this step a backup offered by mail never reaches the hedger.
Every value comes from the facts and every row cites them; nothing is filled in. A value that cannot be read
exactly (a lead time such as "2 working days", a price with an unknown format) is left empty, so the hedger
reports the offer as unusable instead of guessing.
"""
from __future__ import annotations

import json
import logging
import re

import psycopg
from psycopg.types.json import Json

from sourcing import timing

log = logging.getLogger("agents.kb_offers")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ][\d:.+Z-]*)?$")
NUMBER = re.compile(r"^\s*(?:EUR|€)?\s*(\d+(?:[.,]\d{1,2})?)\s*(?:EUR|€)?\s*$", re.I)
PER_UNIT = re.compile(r"^(?:per |/ ?)?(?:1 )?(?:unit|piece|pc|pcs|each|ea)s?$", re.I)


PRICE_EACH = re.compile(r"\s*(?:per\s+|/\s*)(?:1\s+)?(?:unit|piece|pc|pcs|each|ea)s?\s*$", re.I)
AFTER_ORDER = re.compile(r"\s+(?:after|of|from)\s+(?:your\s+)?(?:acceptance|order)\s*$", re.I)


def _number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = NUMBER.match(str(value or ""))
    return float(m.group(1).replace(",", ".")) if m else None


def _hours(value):
    if value in (None, ""):
        return None
    try:
        return timing.parse_hours(AFTER_ORDER.sub("", str(value)))
    except timing.TimingError:
        return None


def offers_from_rows(rows) -> list[dict]:
    """Group `offer` fact rows by subject into plan.offers rows (supplier, part, quantity, price, lead time)."""
    grouped: dict[str, dict] = {}
    for r in rows:
        if r["fact_type"] != "offer":
            continue
        subject = r["subject"] if isinstance(r["subject"], dict) else json.loads(r["subject"] or "{}")
        key = json.dumps(subject, sort_keys=True)
        entry = grouped.setdefault(key, {"subject": subject, "attrs": {}, "sources": []})
        entry["attrs"][r["attribute"]] = r["value"]
        entry["sources"].append({"fact_id": str(r["fact_id"])})
    offers = []
    for entry in grouped.values():
        s, a = entry["subject"], entry["attrs"]
        supplier, part = s.get("supplier_id"), a.get("material_id") or s.get("item")
        if not supplier or not part:
            continue
        ref = "/".join(str(s.get(k) or "") for k in ("supplier_id", "offer_ref", "item"))
        offers.append({
            "offer_id": f"kb:{ref}", "supplier_id": str(supplier), "part_id": str(part),
            "quantity": _number(a.get("qty")),
            "unit_price_eur": (_number(PRICE_EACH.sub("", str(a.get("unit_price") or "")))
                               if PER_UNIT.match(str(a.get("price_basis") or "each")) else None),
            "lead_time_h": _hours(a.get("lead_time")) if a.get("lead_time") else _hours(a.get("ready_time")),
            "valid_until": a.get("valid_until") if ISO_DATE.match(str(a.get("valid_until") or "")) else None,
            "sources": sorted(entry["sources"], key=lambda x: x["fact_id"])})
    return offers


def sync(kb_conn, plan_conn, rows=None) -> list[str]:
    """Insert the offers the kb holds that plan.offers does not have yet. Returns the new offer ids."""
    if rows is None:
        rows = kb_conn.execute("SELECT fact_id, fact_type, subject, attribute, value FROM current_facts "
                               "WHERE fact_type = 'offer'").fetchall()
        kb_conn.commit()
    added = []
    for o in offers_from_rows(rows):
        if o["quantity"] is not None and o["quantity"] <= 0:
            continue
        try:
            with plan_conn.transaction():       # one unreadable offer must not lose the others
                cur = plan_conn.execute(
                    "INSERT INTO offers (offer_id, supplier_id, part_id, quantity, unit_price_eur, lead_time_h, "
                    "valid_until, sources) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (offer_id) DO NOTHING",
                    (o["offer_id"], o["supplier_id"], o["part_id"], o["quantity"], o["unit_price_eur"],
                     o["lead_time_h"], o["valid_until"], Json(o["sources"])))
        except (psycopg.DataError, psycopg.IntegrityError) as exc:
            log.warning("offer %s not stored (%s)", o["offer_id"], type(exc).__name__)
            continue
        if cur.rowcount:
            added.append(o["offer_id"])
            log.info("offer %s from %d kb facts", o["offer_id"], len(o["sources"]))
    plan_conn.commit()
    return added
