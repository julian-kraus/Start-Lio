"""The recipient allowlist. Built from Soony master data (kb supplier facts, or profile.csv as a fallback) plus team
test addresses. Addresses found inside emails are never added automatically."""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path

from psycopg import sql

from . import db
from .config import MailSettings

EMAIL = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[a-z]{2,}$", re.I)
SUPPLIER_ID = re.compile(r"^SUP-\d{4}$")


def from_kb(conn, s: MailSettings) -> dict[str, dict] | None:
    if not conn.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (f"{s.kb_schema}.facts",)).fetchone()["ok"]:
        return None
    rows = conn.execute(sql.SQL(
        "SELECT DISTINCT ON (subject->>'supplier_id', attribute) subject->>'supplier_id' AS supplier_id, attribute, "
        "value #>> '{{}}' AS value, fact_id, source_path FROM {}.facts WHERE valid AND fact_type = 'supplier' "
        "AND attribute = ANY(%s) AND subject ? 'supplier_id' "
        "ORDER BY subject->>'supplier_id', attribute, observed_at DESC").format(sql.Identifier(s.kb_schema)),
        ([s.kb_email_attr, s.kb_language_attr, s.kb_name_attr],)).fetchall()
    per: dict[str, dict] = {}
    for r in rows:
        per.setdefault(r["supplier_id"], {})[r["attribute"]] = r
    out = {}
    for sup, attrs in per.items():
        e = attrs.get(s.kb_email_attr)
        if not e or not e["value"] or not EMAIL.match(e["value"].strip()):
            continue
        out[e["value"].strip().lower()] = {
            "supplier_id": sup,
            "language": (attrs.get(s.kb_language_attr) or {}).get("value"),
            "display_name": (attrs.get(s.kb_name_attr) or {}).get("value"),
            "source": f"kb:fact:{e['fact_id']}",
        }
    return out or None


def from_profiles(data_dir: Path) -> dict[str, dict]:
    """Fallback without kb: find columns by content (an email cell, a SUP-#### cell), not by fixed names."""
    out = {}
    for path in sorted((data_dir / "current" / "suppliers").glob("*/profile.csv")):
        text = path.read_bytes().decode("utf-8-sig", errors="replace")
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        rows = list(csv.reader(io.StringIO(text), dialect))
        if len(rows) < 2:
            continue
        header = [h.strip().lower() for h in rows[0]]
        lang_col = next((i for i, h in enumerate(header) if "lang" in h), None)
        name_col = next((i for i, h in enumerate(header) if "contact" in h and "name" in h), None)
        for row in rows[1:]:
            emails = [c.strip().lower() for c in row if EMAIL.match(c.strip())]
            sups = [c.strip() for c in row if SUPPLIER_ID.match(c.strip())]
            if len(emails) != 1 or len(sups) != 1:
                continue
            out[emails[0]] = {
                "supplier_id": sups[0],
                "language": row[lang_col].strip() if lang_col is not None and lang_col < len(row) else None,
                "display_name": row[name_col].strip() if name_col is not None and name_col < len(row) else None,
                "source": f"profile:{path.relative_to(data_dir / 'current').as_posix()}",
            }
    return out


def sync(conn, s: MailSettings, actor: str = "mail:contacts") -> dict:
    master = from_kb(conn, s)
    origin = "kb"
    if master is None:
        master, origin = from_profiles(s.tracker_data_dir), "profile"
    wanted = dict(master)
    for addr in s.test_addresses:
        wanted[addr] = {"supplier_id": None, "language": None, "display_name": "TEST supplier",
                        "source": "env:MAIL_TEST_ADDRESSES", "is_test": True}
    counts = {"origin": origin, "added": 0, "changed": 0, "revoked": 0, "unchanged": 0}
    current = {r["address"]: r for r in conn.execute("SELECT * FROM contacts")}
    for addr, c in wanted.items():
        is_test = bool(c.get("is_test"))
        old = current.get(addr)
        if old is None:
            conn.execute("INSERT INTO contacts (address, supplier_id, display_name, language, source, is_test) "
                         "VALUES (%s,%s,%s,%s,%s,%s)",
                         (addr, c["supplier_id"], c["display_name"], c["language"], c["source"], is_test))
            db.audit(conn, actor, "contact_added", "contact", addr, {**c, "is_test": is_test})
            counts["added"] += 1
        elif (old["supplier_id"], old["language"], old["display_name"], old["is_test"], old["revoked_at"]) != \
                (c["supplier_id"], c["language"], c["display_name"], is_test, None):
            conn.execute("UPDATE contacts SET supplier_id=%s, display_name=%s, language=%s, source=%s, is_test=%s, "
                         "revoked_at=NULL, updated_at=now() WHERE address=%s",
                         (c["supplier_id"], c["display_name"], c["language"], c["source"], is_test, addr))
            db.audit(conn, actor, "contact_changed", "contact", addr,
                     {"before": {k: old[k] for k in ("supplier_id", "language", "is_test", "revoked_at")}, "after": c})
            counts["changed"] += 1
        else:
            counts["unchanged"] += 1
    for addr, old in current.items():
        auto = old["source"].startswith(("kb:", "profile:", "env:"))
        if auto and addr not in wanted and old["revoked_at"] is None:
            conn.execute("UPDATE contacts SET revoked_at = now(), updated_at = now() WHERE address = %s", (addr,))
            db.audit(conn, actor, "contact_revoked", "contact", addr, {"reason": "no longer in master data"})
            counts["revoked"] += 1
    return counts


def add_manual(conn, address: str, supplier_id: str | None, is_test: bool, actor: str, language: str | None = None):
    address = address.strip().lower()
    if not EMAIL.match(address):
        raise ValueError(f"not an email address: {address!r}")
    if supplier_id and not SUPPLIER_ID.match(supplier_id):
        raise ValueError(f"not a supplier id: {supplier_id!r}")
    conn.execute("INSERT INTO contacts (address, supplier_id, language, source, is_test) VALUES (%s,%s,%s,%s,%s) "
                 "ON CONFLICT (address) DO UPDATE SET supplier_id=EXCLUDED.supplier_id, language=EXCLUDED.language, "
                 "source=EXCLUDED.source, is_test=EXCLUDED.is_test, revoked_at=NULL, updated_at=now()",
                 (address, supplier_id, language, f"manual:{actor}", is_test))
    db.audit(conn, actor, "contact_added_manual", "contact", address,
             {"supplier_id": supplier_id, "is_test": is_test, "language": language})


def revoke(conn, address: str, actor: str) -> bool:
    cur = conn.execute("UPDATE contacts SET revoked_at = now(), updated_at = now() WHERE address = %s "
                       "AND revoked_at IS NULL", (address.strip().lower(),))
    if cur.rowcount:
        db.audit(conn, actor, "contact_revoked", "contact", address, {"reason": "manual"})
    return bool(cur.rowcount)
