"""A synthetic policy, two org charts and their org_seat facts. Everything here is invented."""

POLICY = """Example Co · AP-1 Approval rules · v2.0
Version 2.0 · Effective 2030-01-01 · Owner: Purchasing Director
1. Purpose
This policy says who may approve orders.
Budgets are set by the Chief Treasury Officer.
2. Approval limits by value (per order, in EUR)
Role
Limit per order
Planner
< 700
Area Manager
< 7,000
Purchasing Director
< 63 100
Chief Supply Officer
< 700.000
CSO and CTO jointly
≥ 700,000
3. Receiving site
Orders delivered to a site require, in addition to the value approval, a booked slot released by the warehouse
chief.
4. Air freight
Unplanned air freight up to EUR 3,500 per calendar month and category is approved by the responsible
Area Manager. Above that, approval by the CSO is required before booking.
5. New suppliers
A supplier is released only after a test and written release by the Test
Engineer and the responsible Area Manager. Conditional releases are limited to 30 days from the date of
release. Orders placed after a conditional release has expired are held. A conditional
release is extended in writing, once and by at most 15 days, by the responsible Area Manager together with the
Test Engineer.
6. Exceptions
Exceptions must be documented and approved by the Purchasing Director."""

CHART_V1 = """Example Co · Organisation (v1, 2030-01-05)
Internal. Limits see the approval rules.
Ada Able
Chief Supply Officer
Dr. Ben Baker
Purchasing Director
Cleo Cole
Area Manager Paper & Board
TBD
Area Manager Toys
Dan Dunn
Senior Planner (Paper & Board)
Eve East
Test Engineer
North Depot (dotted line to Purchasing Director)
Finn Ford
Warehouse Chief North Depot
Also: Gus Gray (IT Lead (Purchasing)), Hana Hill (Chief Treasury Officer)"""

CHART_V2 = (CHART_V1.replace("(v1, 2030-01-05)", "(v2, 2030-03-10)")
            .replace("TBD", "Ira Iles (from 2030-03-12)").replace("Cleo Cole", "Jo James"))


def seat(title, holder="", status="held", *, scope="", start="", end=""):
    return {"title": title, "scope": scope, "holder": holder, "status": status, "valid_from": start,
            "valid_until": end}


SEATS_V1 = [
    seat("Chief Supply Officer", "Ada Able"),
    seat("Purchasing Director", "Dr. Ben Baker"),
    seat("Area Manager Paper & Board", "Cleo Cole", scope="Paper & Board"),
    seat("Area Manager Toys", status="vacant", scope="Toys"),
    seat("Senior Planner (Paper & Board)", "Dan Dunn", scope="Paper & Board"),
    seat("Test Engineer", "Eve East"),
    seat("Warehouse Chief North Depot", "Finn Ford", scope="North Depot"),
    seat("IT Lead (Purchasing)", "Gus Gray"),
    seat("Chief Treasury Officer", "Hana Hill"),
]


def changed(seats, which, /, **change):
    assert [s["title"] for s in seats].count(which) == 1, which
    return [{**s, **change} if s["title"] == which else s for s in seats]


SEATS_V2 = changed(changed(SEATS_V1, "Area Manager Paper & Board", holder="Jo James"),
                   "Area Manager Toys", holder="Ira Iles", status="held", valid_from="2030-03-12")


def facts(text, path, kind, *, scope=None, extractor="test:1", mail=None):
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        locator = {"page": 1, "line": i, **({"mail": mail} if mail else {})}
        subject = {"scope": scope} if scope else {"source_path": path, "line_key": f"L{i}"}
        out.append({"fact_id": f"{path}|{extractor}|{i}", "path": path, "sha256": path, "doc_kind": kind,
                    "fact_type": "policy_directive" if scope else "x_document_line", "extractor": extractor,
                    "subject": subject, "attribute": "directive" if scope else "text",
                    "value": line, "locator": locator, "confidence": 0.9})
    return out


def seat_facts(seats, path, text="", *, extractor="test:1", mail=None, kind="org_chart"):
    """One fact per attribute, as the org_seat contract has them."""
    lines = text.splitlines()
    out = []
    for n, row in enumerate(seats):
        key = next((f"L{i}" for i, line in enumerate(lines, 1) if row["holder"] and row["holder"] in line), "")
        subject = {"source_path": path, "title": row["title"], "scope": row["scope"]}
        for name in ("holder", "status", "valid_from", "valid_until", "line_key"):
            out.append({"fact_id": f"{path}|{extractor}|seat{n}|{name}", "path": path, "sha256": path,
                        "doc_kind": kind, "fact_type": "org_seat", "extractor": extractor, "subject": subject,
                        "attribute": name, "value": row.get(name, key), "confidence": 0.9,
                        "locator": {"page": 1, **({"mail": mail} if mail else {})}})
    return out


def chart_facts(text, seats, path, **how):
    return facts(text, path, "org_chart", **how) + seat_facts(seats, path, text, **how)


def world(policy=POLICY, *charts):
    """`charts` are (text, seats) pairs, by default the two versions."""
    rows = facts(policy, "own/policy.pdf", "policy_document", scope="AP-1")
    for i, (text, seats) in enumerate(charts or ((CHART_V1, SEATS_V1), (CHART_V2, SEATS_V2))):
        rows += chart_facts(text, seats, f"own/chart{i}.pdf")
    return rows


def newest(which, /, **change):
    """The world in which one seat of the only chart differs."""
    return world(POLICY, (CHART_V2, changed(SEATS_V2, which, **change)))
