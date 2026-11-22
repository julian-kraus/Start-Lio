"""Readings that must never end in a named signer. Same synthetic facts as test_approvals."""

import pytest

from approvals_world import CHART_V2, POLICY, SEATS_V2, changed, chart_facts, facts, newest, world
from test_approvals import route

AREA = "Area Manager Paper & Board"
UNDATED = POLICY.replace(" · Effective 2030-01-01", "")
NEWER = POLICY.replace("v2.0", "v3.0").replace("Version 2.0", "Version 3.0").replace("2030-01-01", "2030-02-01")

# Wordings that a reading by the shape of the line turned into a signer, or could have.
VACANT = ["Not Filled", "Seat Empty", "Search Ongoing", "Offene Stelle", "To be appointed", "To Be Announced",
          "Not yet appointed", "Vacancy", "Vacant position", "Position vacant", "Open role", "Unassigned",
          "Unfilled", "N.N", "TBD.", "Recruiting", "None", "Nobody", "Pending"]
ABSENT = ["Jo James Retiring", "Jo James Unavailable", "Jo James, on leave", "Jo James - on leave",
          "Jo James On Leave", "Jo James retired", "Jo James (on leave)", "Jo James until March"]
BELOW_TITLE = ["Out of office", "Left the company", "on leave", "(on leave until April)", "- absent"]
HEADINGS = ["Western Europe", "Greenfield Campus", "Operations", "Executive Board", "Logistics Coordinator",
            "Interim", "Acting Manager", "Position under review", "Jo James and Kim King"]
PROBES = ([(CHART_V2.replace("Jo James", text), text, "vacant") for text in VACANT]
          + [(CHART_V2.replace("Jo James", text), text, "absent") for text in ABSENT]
          + [(CHART_V2.replace(AREA, f"{AREA}\n{text}"), "Jo James", "absent") for text in BELOW_TITLE]
          + [(CHART_V2.replace("Jo James", text), text, "unclear") for text in HEADINGS])
ACTIONS = [("purchase_order", 1000), ("conditional_release", None), ("release_extension", None)]


@pytest.mark.parametrize("action, amount", ACTIONS)
@pytest.mark.parametrize("text, holder, status", PROBES, ids=lambda value: None)
def test_probed_wording_never_signs(text, holder, status, action, amount):
    # with the seat as extraction has to state it, also when it wrongly copies the wording into the holder
    seats = changed(SEATS_V2, AREA, holder=holder, status=status)
    out = route(world(POLICY, (text, seats)), action=action, amount=amount, extensions_used=0)
    assert out["status"] == "needs_human" and "name" not in out["signer"]


@pytest.mark.parametrize("text, holder, status", PROBES, ids=lambda value: None)
def test_probed_wording_never_signs_without_a_seat_fact(text, holder, status):
    seats = [s for s in SEATS_V2 if s["title"] != AREA]
    out = route(world(POLICY, (text, seats)))
    assert out["status"] == "needs_human" and "name" not in out["signer"]


def test_vacancy_at_the_top_tier_and_in_air_freight_needs_a_human():
    rows = newest("Chief Supply Officer", holder="Not Filled", status="vacant")
    for how in ({"amount": 700000}, {"action": "air_freight", "amount": 9000}):
        out = route(rows, **how)
        assert out["status"] == "needs_human" and "name" not in out["signer"]


def second_policy(text, scope="AP-1", path="own/policy3.pdf"):
    return world() + facts(text, path, "policy_document", scope=scope)


@pytest.mark.parametrize("holder", ["Minh To", "Tobe Nwigwe", "Nona Vance", "Pia Openshaw"])
def test_names_that_resemble_placeholders_still_sign(holder):
    out = route(newest(AREA, holder=holder))
    assert out["status"] == "resolved"
    assert out["signer"]["name"] == holder


@pytest.mark.parametrize("rows", [
    world(UNDATED),
    second_policy(NEWER.replace(" · Effective 2030-02-01", "")),
    second_policy(NEWER.replace("2030-02-01", "2030-02-31")),
], ids=["only policy", "beside a dated one", "impossible date"])
def test_policy_without_a_readable_date_needs_a_human(rows):
    out = route(rows)
    assert out["status"] == "needs_human"
    assert any("effective date" in reason for reason in out["reasons"])


@pytest.mark.parametrize("effective", ["2030-02-01", "2029-06-01"])
@pytest.mark.parametrize("scope", ["TR-9", None])
def test_limits_of_another_policy_need_a_human(effective, scope):
    other = NEWER.replace("AP-1 Approval rules", "TR-9 Travel rules").replace("2030-02-01", effective)
    out = route(second_policy(other, scope, "own/travel.pdf"))
    assert out["status"] == "needs_human"
    assert any("different policies" in reason for reason in out["reasons"])


@pytest.mark.parametrize("scope", ["AP-1", None])
def test_new_version_of_the_same_policy_still_wins(scope):
    out = route(second_policy(NEWER, scope))
    assert out["status"] == "resolved"
    assert out["policy"]["version"] == "3.0"


def test_versions_without_a_scope_are_one_policy():
    rows = facts(POLICY, "own/policy.pdf", "policy_document") + facts(NEWER, "own/policy3.pdf", "policy_document")
    out = route(rows + chart_facts(CHART_V2, SEATS_V2, "own/chart.pdf"))
    assert out["status"] == "resolved"
    assert out["policy"]["version"] == "3.0"
