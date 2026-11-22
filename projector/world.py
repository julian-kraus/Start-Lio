"""The computed world a scenario runs on: kb read once, every model of sourcing/ applied to it. No LLM, no writes.

    w = World.load(conn, scenario)
    w.coverage, w.spots, w.cards, w.signer(amount_eur, on)

Every number the projector shows comes from here or from a labelled scenario input.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone

from psycopg.rows import dict_row

from sourcing import approvals, coverage, coverage_windows, overbook, reliability, weakspots
from sourcing.facts import Facts, day

FACT_COLUMNS = ("f.fact_id, f.path, f.fact_type, f.subject, f.attribute, f.value, f.as_of, f.confidence, "
                "d.doc_kind, d.sha256")


def instant(text: str) -> datetime:
    return datetime.fromisoformat(str(text))


def goal_of(scenario: dict) -> dict:
    """The goal as the screen records it from the interview, with the edition the scenario is about."""
    g = scenario["goal"]
    return {"statement": g["statement"], "edition": scenario["edition"], "unit_loss_eur": g["unit_loss_eur"],
            "sacrifice": g["sacrifice"], "budget_eur": g["budget_eur"]}


@dataclass
class World:
    rows: list[dict]
    believed: list[dict]
    facts: Facts
    history: reliability.History
    windows: list
    zone: timezone
    profiles: dict      # kb.supplier_profiles: delivery record, shipments and top issues per supplier

    @classmethod
    def load(cls, conn, schema: str = "kb") -> "World":
        cur = conn.cursor(row_factory=dict_row)
        rows = cur.execute(f"SELECT {FACT_COLUMNS} FROM {schema}.current_facts f "
                           f"LEFT JOIN {schema}.documents d ON d.path = f.path AND d.active").fetchall()
        believed = cur.execute(f"SELECT rule_id, key, value, winning_fact_ids, losing_fact_ids, contradiction "
                               f"FROM {schema}.believed").fetchall()
        issues = cur.execute(f"SELECT kind, payload, fact_ids FROM {schema}.issues WHERE state = 'open'").fetchall()
        facts = Facts(rows, believed, issues)
        profiles = {r["supplier_id"]: r["profile"] for r in
                    cur.execute(f"SELECT supplier_id, profile FROM {schema}.supplier_profiles").fetchall()}
        launch = max(d for r in facts.rows("product") if (d := day(r.get("launch_date"))))
        zone = timezone(timedelta(hours=1))
        end = datetime.combine(launch, time(23, 59), zone)
        windows = coverage_windows.pack_windows(facts, end, end)
        return cls(rows, believed, facts, reliability.history(rows, believed), windows, zone, profiles)

    @property
    def deadline(self) -> datetime:
        return self.windows[-1].end

    def coverage(self, now: datetime) -> dict:
        return coverage.compute(self.facts, now=now, deadline=self.deadline)

    def spots(self, cov: dict, goal: dict) -> list[dict]:
        return weakspots.rank(self.facts, cov, goal)

    def card(self, supplier_id: str, promises=()) -> dict:
        return reliability.card(self.history, supplier_id, promises)

    def suppliers(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for r in self.facts.rows("supplier"):
            sid = r.get("supplier_id")
            if sid:
                out[str(sid)] = {k: r.get(k) for k in ("name", "city", "country", "preferred_language",
                                                         "contact_name", "notes", "qualification_status")}
        return out

    def parts_of(self, supplier_id: str) -> list[str]:
        found = []
        for r in self.facts.rows("supplier_part"):
            if str(r.get("supplier_id")) == supplier_id:
                share = r.get("share")
                pct = f" ({float(share):.0%})" if share not in (None, "") else ""
                found.append(f"{str(r.get('description') or r.get('material_id')).split('(')[0].strip()}{pct}")
        return found

    def part_name(self, part_id: str) -> str:
        for r in self.facts.rows("component"):
            if str(r.get("material_id")) == part_id and r.get("description"):
                return str(r.get("description")).split("(")[0].strip()
        return part_id

    def price(self, supplier_id: str, part_id: str, on: datetime):
        return overbook.price(self.rows, supplier_id, part_id, on=on.date())

    def site(self) -> dict:
        sites = self.facts.rows("site")
        return {"site_id": sites[0].get("site_id"), "name": sites[0].get("name")} if sites else {}

    def product(self, edition_name: str, cov: dict) -> dict:
        for e in cov["editions"]:
            if str(e["name"]).casefold() == edition_name.casefold():
                return e
        raise KeyError(f"no edition {edition_name!r} in coverage")

    def signer(self, amount_eur: float, amount_source: dict, on: datetime, category: str | None,
               site_id: str | None) -> dict:
        r = approvals.route(self.rows, action="purchase_order", on=on, amount_eur=amount_eur,
                            amount_source=amount_source, category=category, delivery_site=site_id)
        signer = dict(r.get("signer") or {})
        signer["status"] = r.get("status")
        signer["reasons"] = r.get("reasons", [])
        return signer

    def buyer_of(self, supplier_id: str, part_id: str) -> tuple[str | None, list[str]]:
        """The buyer who placed this supplier's orders for this part, from the PO history, with the fact ids."""
        counts: dict[str, list[str]] = {}
        for r in self.facts.rows("po_line"):
            if str(r.get("supplier_id")) == supplier_id and str(r.get("material_id")) == part_id and r.get("buyer"):
                counts.setdefault(str(r.get("buyer")), []).append(r.sources.get("buyer", ""))
        if not counts:
            return None, []
        name = max(counts, key=lambda k: len(counts[k]))
        return name, [f for f in counts[name] if f][:10]
