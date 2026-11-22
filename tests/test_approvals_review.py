"""Paths that named a signer the facts do not support, each next to the reading that does sign."""

from datetime import date

import pytest

from approvals_world import CHART_V1, CHART_V2, POLICY, SEATS_V1, SEATS_V2, changed, facts, newest, seat, seat_facts, world
from test_approvals import route

AREA = "Area Manager Paper & Board"
NEWER = (POLICY.replace("v2.0", "v3.0").replace("Version 2.0", "Version 3.0").replace("2030-01-01", "2030-02-01")
         .replace("< 7,000", "< 900"))


def unnamed(out):
    return out["status"] == "needs_human" and "name" not in out["signer"]


def beside_v2(text, scope="AP-1", **change):
    newer = facts(text, "own/policy3.pdf", "policy_document", scope=scope)
    return world() + [{**r, **change} for r in newer]


def test_readable_newer_policy_signs_by_its_limits():
    assert route(beside_v2(NEWER))["signer"]["name"] == "Dr. Ben Baker"


@pytest.mark.parametrize("shift", range(12))
def test_newer_policy_without_a_reading_order_never_falls_back(shift):
    newer = facts(NEWER, "own/policy3.pdf", "policy_document", scope="AP-1")
    newer = [{**r, "locator": {"page": 1}} for r in newer][::-1]
    out = route(world() + newer[shift:] + newer[:shift])
    assert unnamed(out) and "policy3" in " ".join(out["reasons"])


@pytest.mark.parametrize("scope", ["AP-1", "AP-3", None])
def test_newer_policy_with_another_heading_never_falls_back(scope):
    text = NEWER.replace("Approval limits by value", "Approval thresholds by value")
    assert unnamed(route(beside_v2(text, scope)))


def test_newer_policy_with_a_misread_heading_never_falls_back():
    text = NEWER.replace("2. Approval limits by value", "2. Approvel lirnits by value")
    assert unnamed(route(beside_v2(text, "AP-3", confidence=0.4)))


def test_unrelated_policy_without_limits_does_not_block():
    other = "Example Co · TR-9 Travel rules\n1. Purpose\nBook trains early."
    assert route(beside_v2(other, "TR-9"))["status"] == "resolved"


def test_policy_that_states_two_effective_dates_needs_a_human():
    text = NEWER.replace("Version 3.0 ·", "Replaces the rules effective 2029-06-01 · Version 3.0 ·")
    assert unnamed(route(beside_v2(text)))


@pytest.mark.parametrize("grade", ["Junior", "Acting", "Principal", "Deputy"])
@pytest.mark.parametrize("title, how", [("Purchasing Director", {"amount": 7000}),
                                        ("Test Engineer", {"action": "conditional_release", "amount": None})])
def test_another_grade_never_signs_as_the_role(grade, title, how):
    assert unnamed(route(newest(title, title=f"{grade} {title}"), **how))


def test_company_wide_seat_does_not_stand_in_for_a_category_seat():
    seats = changed(SEATS_V2, "Area Manager Toys", title="Area Manager Toys & Games", scope="Toys & Games",
                    holder="", status="vacant")
    assert unnamed(route(world(POLICY, (CHART_V2, seats + [seat("Area Manager", "Olu Obi")])), category="Toys"))


def test_company_wide_seat_signs_where_the_role_is_not_held_per_category():
    seats = [s for s in SEATS_V2 if not s["title"].startswith("Area Manager")] + [seat("Area Manager", "Olu Obi")]
    assert route(world(POLICY, (CHART_V2, seats)), category="Toys")["signer"]["name"] == "Olu Obi"


@pytest.mark.parametrize("scope, extractor", [("", "test:1"), ("", "llm:test"), ("Paper", "llm:test")])
def test_reading_that_missed_the_scope_still_disagrees(scope, extractor):
    second = changed(SEATS_V2, AREA, title="Area Manager", scope=scope, status="absent")
    first = changed(SEATS_V2, AREA, title="Area Manager", scope="Paper & Board")
    rows = world(POLICY, (CHART_V2, first))
    assert unnamed(route(rows + seat_facts(second, "own/chart0.pdf", extractor=extractor)))


def test_site_reading_that_missed_the_scope_still_disagrees():
    rows = world(POLICY, (CHART_V2, SEATS_V2 + [seat("Warehouse Chief", status="absent")]))
    assert unnamed(route(rows, delivery_site="North Depot"))


@pytest.mark.parametrize("header", ["(v3, replaces 2030-01-05, valid 2030-03-11)",
                                    "(v3, 2030-03-11, replaces 2030-01-05)",
                                    "(v3, replaces 2030-01-05, valid 11 March 2030)",
                                    "(v3, replaces 2030-01-05, valid 11.03.30)",
                                    "(v3, replaces 2030-01-05)\nValid 2030-03-11"])
def test_chart_header_with_two_dates_is_not_guessed(header):
    newer = CHART_V2.replace("(v2, 2030-03-10)", header)
    seats = changed(SEATS_V2, AREA, holder="Kim King")
    assert unnamed(route(world(POLICY, (CHART_V1, SEATS_V1), (CHART_V2, SEATS_V2), (newer, seats))))


def test_newer_chart_without_a_reading_order_never_falls_back():
    rows = world()
    assert unnamed(route([{**r, "locator": {"page": 1}} if r["path"] == "own/chart0.pdf" else r for r in rows]))


@pytest.mark.parametrize("title", ["Chief Sales Officer", "Chief Security Officer"])
def test_abbreviation_is_not_any_chief_with_that_initial(title):
    assert unnamed(route(newest("Chief Supply Officer", title=title), action="air_freight", amount=9000))


def test_abbreviation_the_policy_never_writes_out_needs_a_human():
    policy = POLICY.replace("Budgets are set by the Chief Treasury Officer.\n", "")
    assert policy != POLICY
    assert unnamed(route(world(policy), amount=700000))


def test_abbreviation_the_policy_writes_out_two_ways_needs_a_human():
    policy = POLICY.replace("1. Purpose\n", "1. Purpose\nThe Chief Sales Officer is informed.\n")
    assert unnamed(route(world(policy), action="air_freight", amount=9000))


def test_seat_titled_with_the_abbreviation_signs():
    out = route(newest("Chief Supply Officer", title="CSO"), action="air_freight", amount=9000)
    assert out["signer"]["name"] == "Ada Able"


def test_negative_extension_count_is_refused():
    with pytest.raises(ValueError, match="extensions_used"):
        route(action="release_extension", amount=None, extensions_used=-1)


def test_extension_of_an_expired_release_is_held():
    out = route(action="release_extension", amount=None, extensions_used=0,
                release_valid_until=date(2030, 3, 10),
                release_source="test")
    assert out["status"] == "held" and "name" not in out["signer"]
