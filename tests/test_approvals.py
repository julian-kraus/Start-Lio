"""Signer routing on synthetic facts. Every role, name, date and amount here is invented."""

from datetime import date, datetime, timezone

import pytest

from plan import contracts
from sourcing import approvals
from sourcing.approval_docs import number

from approvals_world import CHART_V1, CHART_V2, POLICY, SEATS_V1, SEATS_V2, chart_facts, changed, facts, newest, world

DAY = date(2030, 3, 11)
SOURCE = {"scenario_input": "test amount"}


def route(rows=None, action="purchase_order", amount=1000, category="Paper & Board", on=DAY, **kw):
    out = approvals.route(world() if rows is None else rows, action=action, on=on, amount_eur=amount,
                          amount_source=SOURCE, category=category, **kw)
    contracts.validate("decision", {"decision_id": "D", "status": "open", "title": "t", "signer": out["signer"],
                                    "sources": out["sources"]})
    return out


def air(amount, used=None, **kw):
    if used is not None:
        kw.update(budget_used_eur=used, budget_used_source=SOURCE)
    return route(action="air_freight", amount=amount, **kw)


@pytest.mark.parametrize("amount, role, name", [
    (0, "Senior Planner (Paper & Board)", "Dan Dunn"),
    (699.99, "Senior Planner (Paper & Board)", "Dan Dunn"),
    (700, "Area Manager Paper & Board", "Jo James"),
    (6999.99, "Area Manager Paper & Board", "Jo James"),
    (7000, "Purchasing Director", "Dr. Ben Baker"),
    (63100, "Chief Supply Officer", "Ada Able"),
    (699999.99, "Chief Supply Officer", "Ada Able"),
    (700000, "Chief Supply Officer and Chief Treasury Officer", "Ada Able and Hana Hill"),
])
def test_value_tiers(amount, role, name):
    out = route(amount=amount)
    assert out["status"] == "resolved"
    assert (out["signer"]["role"], out["signer"]["name"]) == (role, name)
    assert "AP-1 v2.0 section 2" in out["signer"]["rule"]


def test_tiers_follow_the_facts():
    moved = POLICY.replace("< 700\n", "< 1,500\n")
    assert route(world(moved), amount=1000)["signer"]["name"] == "Dan Dunn"


def test_every_number_has_its_source():
    out = route(amount=1000)
    cited = {s.get("fact_id") for s in out["sources"]}
    assert out["limit"]["value"] == 7000 and set(out["limit"]["fact_ids"]) <= cited
    assert out["amount"]["source"] == SOURCE and SOURCE in out["sources"]
    with pytest.raises(ValueError, match="source"):
        approvals.route(world(), action="purchase_order", on=DAY, amount_eur=5)


def test_newer_chart_wins_and_is_named():
    out = route()
    assert out["signer"]["name"] == "Jo James"
    assert out["org_chart"]["label"] == "org chart v2 of 2030-03-10"
    assert [c["version"] for c in out["org_chart"]["replaces"]] == ["1"]


def test_newer_chart_wins_in_any_input_order():
    assert route(world()[::-1]) == {**route(), "reasons": []}


def test_chart_dated_later_does_not_apply_yet():
    out = route(on=date(2030, 3, 9))
    assert out["signer"]["name"] == "Cleo Cole" and out["org_chart"]["version"] == "1"


def test_same_version_different_content_needs_a_human():
    other = changed(SEATS_V2, "Area Manager Paper & Board", holder="Kim King")
    out = route(world(POLICY, (CHART_V2, SEATS_V2), (CHART_V2, other)))
    assert out["status"] == "needs_human" and "name" not in out["signer"]


def test_undated_chart_could_be_newer_so_needs_a_human():
    undated = CHART_V2.replace(" (v2, 2030-03-10)", "")
    assert route(world(POLICY, (CHART_V1, SEATS_V1), (undated, SEATS_V2)))["status"] == "needs_human"


@pytest.mark.parametrize("on, state", [(date(2030, 3, 9), "vacant"), (DAY, "not_started")])
def test_vacant_role_goes_to_a_human_with_the_exception_rule(on, state):
    out = route(category="Toys", on=on)
    assert out["status"] == "needs_human" and "name" not in out["signer"]
    only = out["signers"][0]
    assert (only["state"], only["role"]) == (state, "Area Manager Toys") and "name" not in only
    assert only["exception_approver"]["role"] == "Purchasing Director"
    assert "section 6" in only["exception_approver"]["rule"]
    cited = {s.get("fact_id") for s in out["sources"]}
    assert set(only["seat_fact_ids"] + only["exception_approver"]["fact_ids"][:1]) <= cited
    if state == "not_started":
        assert "starts 2030-03-12" in " ".join(out["reasons"])


def test_holder_counts_from_the_start_date():
    out = route(category="Toys", on=date(2030, 3, 12))
    assert (out["status"], out["signer"]["name"]) == ("resolved", "Ira Iles")


def test_day_is_the_day_of_the_offset_passed():
    team = datetime.fromisoformat("2030-03-12T00:30:00+01:00")
    assert route(category="Toys", on=team)["signer"]["name"] == "Ira Iles"
    assert route(category="Toys", on=team.astimezone(timezone.utc))["status"] == "needs_human"


def test_holder_ends_after_the_until_date():
    out = route(newest("Area Manager Paper & Board", valid_until="2030-03-10"))
    assert out["status"] == "needs_human" and out["signers"][0]["state"] == "ended"
    assert route(newest("Area Manager Paper & Board", valid_until="2030-03-11"))["status"] == "resolved"


def test_category_without_a_seat_needs_a_human():
    out = route(category="Magic")
    assert out["status"] == "needs_human" and out["signers"][0]["state"] == "not_in_chart"
    assert out["signers"][0]["exception_approver"]["role"] == "Purchasing Director"


def test_unknown_category_is_not_guessed():
    out = route(category=None)
    assert out["status"] == "needs_human" and "name" not in out["signer"]


def test_vacancy_above_the_exception_approver_needs_a_human():
    out = route(newest("Chief Supply Officer", holder="", status="vacant"), amount=81300)
    assert out["status"] == "needs_human" and "name" not in out["signer"]
    assert "lower limit" in " ".join(out["reasons"])
    assert "exception_approver" not in out["signers"][0]


def test_vacant_exception_approver_needs_a_human():
    rows = newest("Purchasing Director", holder="", status="vacant")
    assert route(rows, category="Toys")["status"] == "needs_human"


def test_no_exception_rule_means_no_escalation():
    policy = POLICY.split("6. Exceptions")[0].rstrip()
    out = route(world(policy), category="Toys")
    assert out["status"] == "needs_human" and "exception_approver" not in out["signers"][0]
    assert "no single approver for exceptions" in " ".join(out["reasons"])


def test_unreadable_holder_is_not_named():
    out = route(newest("Area Manager Paper & Board", holder="Jo James (on leave)", status="unclear"))
    assert out["status"] == "needs_human" and "name" not in out["signer"]


def test_air_freight_within_the_month_and_category_limit():
    out = air(500, used=3000)
    assert (out["status"], out["signer"]["name"]) == ("resolved", "Jo James")
    assert out["budget"]["used"] == {"value": 3000, "currency": "EUR", "source": SOURCE}
    assert out["budget"]["limit"]["value"] == 3500


def test_air_freight_above_the_limit_with_what_was_used():
    out = air(501, used=3000)
    assert out["signer"]["name"] == "Ada Able" and "section 4" in out["signer"]["rule"]


def test_air_freight_above_the_limit_alone():
    assert air(3501)["signer"]["name"] == "Ada Able"


def test_air_freight_unknown_budget_is_shown_and_not_assumed_zero():
    out = air(100)
    assert out["budget"]["used"] == "unknown"
    assert out["signer"]["name"] == "Ada Able"
    assert out["budget"]["lower_signer_if_used_at_most"]["value"] == 3400
    assert "unknown" in " ".join(out["reasons"])


def test_air_freight_known_zero_is_not_unknown():
    assert air(100, used=0)["signer"]["name"] == "Jo James"


def test_air_freight_within_limit_needs_the_category():
    assert air(100, used=0, category=None)["status"] == "needs_human"


def test_used_budget_needs_a_source():
    with pytest.raises(ValueError, match="budget_used_eur"):
        route(action="air_freight", amount=1, budget_used_eur=5)


def test_conditional_release_is_signed_by_both_and_limited():
    out = route(action="conditional_release", amount=None)
    assert out["signer"]["name"] == "Eve East and Jo James"
    assert out["validity"]["max_days"] == 30 and out["validity"]["valid_until"] == "2030-04-10"
    assert "section 5" in out["signer"]["rule"]


def test_conditional_release_with_a_vacancy_keeps_both_signers():
    out = route(action="conditional_release", amount=None, category="Toys")
    assert out["status"] == "needs_human" and "name" not in out["signer"]
    assert [s["state"] for s in out["signers"]] == ["held", "not_started"]


@pytest.mark.parametrize("used, status", [(0, "resolved"), (1, "needs_human"), (None, "needs_human")])
def test_release_extension_is_allowed_once(used, status):
    out = route(action="release_extension", amount=None, extensions_used=used)
    assert out["status"] == status and out["validity"]["max_days"] == 15
    assert {s["required_role"] for s in out["signers"]} == {"Area Manager", "Test Engineer"}


@pytest.mark.parametrize("until, status", [(date(2030, 3, 10), "held"), (DAY, "resolved")])
def test_order_after_an_expired_release_is_held(until, status):
    out = route(release_valid_until=until, release_source=SOURCE)
    assert out["status"] == status and ("name" in out["signer"]) == (status == "resolved")


def test_delivery_to_a_site_also_needs_the_site_release():
    out = route(delivery_site="North Depot")
    assert out["signer"]["name"] == "Jo James"
    assert [(s["role"], s["name"]) for s in out["also_required"]] == [("Warehouse Chief North Depot", "Finn Ford")]


@pytest.mark.parametrize("site", ["Example Northern Distribution Center Norwick", "DC-NOR"])
def test_site_is_found_by_its_kb_name_or_id(site):
    seats = changed(SEATS_V2, "Warehouse Chief North Depot", title="Warehouse Chief DC Norwick", scope="DC Norwick")
    known = {"fact_id": "own/sites.csv|1", "path": "own/sites.csv", "doc_kind": "master_table", "fact_type": "site",
             "subject": {"site_id": "DC-NOR"}, "attribute": "name", "confidence": 0.9,
             "value": "Example Northern Distribution Center Norwick"}
    out = route(world(POLICY, (CHART_V2, seats)) + [known], delivery_site=site)
    assert [(s["state"], s["name"]) for s in out["also_required"]] == [("held", "Finn Ford")]


def test_unknown_site_is_not_treated_as_a_vacancy():
    out = route(delivery_site="South Depot")
    assert out["status"] == "needs_human" and "name" not in out["signer"]


@pytest.mark.parametrize("path, mail", [("mail/t/rules.pdf", None), ("suppliers/x/rules.pdf", None),
                                        ("own/rules.pdf", {"trust": "supplier_contact"})])
def test_supplier_sent_documents_never_set_the_rules(path, mail):
    policy = POLICY.replace("2.0", "9.0").replace("2030-01-01", "2030-03-01").replace("< 700\n", "< 5,000\n")
    chart = CHART_V2.replace("(v2,", "(v9,")
    seats = changed(SEATS_V2, "Area Manager Paper & Board", holder="Mal Moss")
    rows = world() + facts(policy, path, "policy_document", scope="AP-1", mail=mail)
    rows += chart_facts(chart, seats, path + ".chart", mail=mail)
    assert route(rows) == route()


def test_other_document_kinds_never_set_the_rules():
    assert route(world() + facts(CHART_V2.replace("(v2,", "(v9,"), "own/notes.pdf", "other_document")) == route()


def test_newer_policy_applies_from_its_effective_date():
    later = POLICY.replace("2.0", "3.0").replace("2030-01-01", "2030-03-11").replace("< 700\n", "< 5,000\n")
    rows = world() + facts(later, "own/policy3.pdf", "policy_document", scope="AP-1")
    assert route(rows)["signer"]["name"] == "Dan Dunn"
    assert route(rows, on=date(2030, 3, 10))["signer"]["name"] == "Jo James"


def test_two_readings_of_the_policy_that_disagree_need_a_human():
    other = facts(POLICY.replace("< 7,000", "< 8,000"), "own/policy.pdf", "policy_document", scope="AP-1",
                  extractor="llm:test")
    assert route(world() + other)["status"] == "needs_human"


def test_two_equal_readings_are_one_policy():
    again = facts(POLICY, "own/policy.pdf", "policy_document", scope="AP-1", extractor="llm:test")
    assert route(world() + again)["status"] == "resolved"


@pytest.mark.parametrize("old, new", [("< 7,000", "< 7,0000"), ("< 7,000", "< 7 million"),
                                      ("< 63 100", "< 60"), (", in EUR)", ")"), (", in EUR)", ", in USD)"), ("< 7,000", "< USD 7,000"),
                                      ("< 63 100", "63,100 - 91,700")])
def test_unreadable_or_foreign_limits_need_a_human(old, new):
    out = route(world(POLICY.replace(old, new)))
    assert out["status"] == "needs_human" and "name" not in out["signer"]


def test_release_date_needs_a_source():
    with pytest.raises(ValueError, match="released_on"):
        route(action="conditional_release", amount=None, released_on=DAY)


def test_limit_words_in_any_case():
    policy = POLICY.replace("≥ 700,000", "From 700,000")
    assert route(world(policy), amount=700000)["signer"]["name"] == "Ada Able and Hana Hill"


def test_role_and_limit_on_one_line():
    policy = POLICY.replace("Planner\n< 700\n", "Planner: < 700\n")
    assert route(world(policy), amount=5)["signer"]["name"] == "Dan Dunn"


@pytest.mark.parametrize("change", [{"confidence": 0.4}, {"locator": None}])
def test_lines_that_cannot_be_trusted_need_a_human(change):
    rows = world()
    rows[0] = {**rows[0], **change}
    assert route(rows)["status"] == "needs_human"


@pytest.mark.parametrize("rows", [[], facts(POLICY, "own/policy.pdf", "policy_document", scope="AP-1"),
                                  chart_facts(CHART_V2, SEATS_V2, "own/chart.pdf")])
def test_missing_facts_need_a_human(rows):
    out = route(rows)
    assert out["status"] == "needs_human" and "name" not in out["signer"]


def test_unknown_amount_needs_a_human():
    assert route(amount=None)["status"] == "needs_human"


@pytest.mark.parametrize("amount", [-1, float("nan"), float("inf"), True, "ten"])
def test_bad_amounts_are_refused(amount):
    with pytest.raises(ValueError):
        route(amount=amount)


def test_bad_action_and_naive_time_are_refused():
    with pytest.raises(ValueError):
        route(action="wire_transfer")
    with pytest.raises(ValueError):
        route(on=datetime(2030, 3, 11, 9))


@pytest.mark.parametrize("text, value", [("12 340", "12340"), ("12 340", "12340"), ("12.340", "12340"),
                                         ("12,340.50", "12340.50"), ("12.340,5", "12340.5"), ("700", "700"),
                                         ("1,23,456", None), ("2m", None)])
def test_numbers_as_printed(text, value):
    assert (None if number(text) is None else str(number(text))) == value
