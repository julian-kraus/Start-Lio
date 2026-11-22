"""Who is in a seat and which seat is a site's. Same synthetic facts as test_approvals."""

import pytest

from test_approvals import CHART_V2, POLICY, route, world

DEPOT = "Warehouse Chief North Depot"


def seated(old, new):
    assert old in CHART_V2
    return world(POLICY, CHART_V2.replace(old, new, 1))


def site(site_id, name, path="own/sites.csv", **change):
    return {"fact_id": f"{path}|{site_id}|{name}", "path": path, "sha256": path, "doc_kind": "master_table",
            "fact_type": "site", "extractor": "test:1", "subject": {"site_id": site_id}, "attribute": "name",
            "value": name, "locator": {}, "confidence": 0.9, **change}


def unnamed(out):
    return out["status"] == "needs_human" and "name" not in out["signer"]


@pytest.mark.parametrize("line", ["Operations", "Logistics Coordinator", "Executive Board", "Customer Services",
                                  "Supply Chain", "Quality Assurance", "Paper Board", "Toys", "North Depot"])
def test_heading_above_an_empty_seat_is_not_its_holder(line):
    assert unnamed(route(seated("Eve East", line), action="conditional_release", amount=None))


def test_board_above_an_empty_top_seat_is_not_its_holder():
    assert unnamed(route(seated("Ada Able", "Executive Board"), amount=100000))


def test_title_of_a_role_outside_the_policy_is_not_a_holder():
    # two lines shaped like names in a row: the second can be a title that no word list knows
    assert unnamed(route(seated("Eve East", "Zed Zane\nCargo Wrangler"), action="conditional_release", amount=None))


def test_heading_above_a_name_does_not_hide_the_name():
    out = route(seated("Eve East", "Executive Board\nEve East"), action="conditional_release", amount=None)
    assert out["status"] == "resolved" and out["signer"]["name"] == "Eve East and Jo James"


@pytest.mark.parametrize("holder", [
    "Jo James, on leave", "Jo James - on leave", "Jo James – on leave", "Jo James, from April",
    "Jo James until March", "Jo James Until March", "Jo James On Leave", "Jo James-On-Leave", "Jo James retired",
    "Jo James and Kim King", "Jo James / Kim King", "Jo James (on leave)", "Jo James*", "Jo", "JO JAMES", "Jo James Deputy",
    "Operations (from 2030-01-01)",
])
def test_qualified_or_unshaped_holder_needs_a_human(holder):
    assert unnamed(route(seated("Jo James", holder)))


@pytest.mark.parametrize("after", ["on leave", "(on leave until April)", "On leave", "- absent"])
def test_qualifier_below_the_title_needs_a_human(after):
    title = "Area Manager Paper & Board"
    assert unnamed(route(seated(title, f"{title}\n{after}")))


@pytest.mark.parametrize("placeholder", ["TBD", "n.n.", "Vacant"])
def test_vacant_seat_below_does_not_spoil_the_seat_above(placeholder):
    assert route(seated("Ira Iles (from 2030-03-12)", placeholder))["signer"]["name"] == "Jo James"


@pytest.mark.parametrize("holder", ["Dr. Ben Baker", "Anna van der Berg", "Liam O'Brien", "Sean McAdam",
                                    "Jean-Luc Moreau", "J. Porter", "Zoë Østby", "Maria de la Cruz",
                                    "Prof. Dr. Lena Vogel"])
def test_names_keep_signing(holder):
    out = route(seated("Jo James", holder))
    assert out["status"] == "resolved" and out["signer"]["name"] == holder


def test_empty_seat_in_the_listed_form_is_not_named():
    assert unnamed(route(seated("Hana Hill (", "Treasury Team ("), amount=700000))


@pytest.mark.parametrize("wanted", ["North Depot Annex", "Depot", "North", "South Depot near North Depot Road"])
@pytest.mark.parametrize("in_kb", [False, True])
def test_another_site_never_gets_this_sites_holder(wanted, in_kb):
    rows = world() + ([site("SD-ANX", wanted)] if in_kb else [])
    assert unnamed(route(rows, delivery_site=wanted))


@pytest.mark.parametrize("wanted", ["DC", "Norwick", "DC-NOR", "Example Northern Distribution Center Norwick"])
def test_part_of_a_name_or_an_id_without_site_facts_needs_a_human(wanted):
    rows = seated(DEPOT, "Warehouse Chief DC Norwick")
    assert unnamed(route(rows, delivery_site=wanted))


@pytest.mark.parametrize("site_id, name", [("DC-NOR", "Example Norwick Distribution Center Annex"),
                                           ("WH-07", "Example Northern Distribution Center Norwick")])
def test_site_that_only_resembles_the_seat_needs_a_human(site_id, name):
    rows = seated(DEPOT, "Warehouse Chief DC Norwick") + [site(site_id, name)]
    assert unnamed(route(rows, delivery_site=site_id))


def test_seat_that_fits_two_sites_needs_a_human():
    rows = seated(DEPOT, "Warehouse Chief DC Norwick") + [site("DC-NOR", "Example Distribution Center Norwick"),
                                                         site("DC-NRW", "DC Norwick")]
    assert unnamed(route(rows, delivery_site="DC-NOR"))


@pytest.mark.parametrize("change", [{"path": "suppliers/x/sites.csv"}, {"path": "mail/t/sites.csv"},
                                    {"locator": {"mail": {"trust": "supplier_contact"}}}, {"confidence": 0.4}])
def test_untrusted_site_fact_links_no_holder(change):
    rows = seated(DEPOT, "Warehouse Chief DC Norwick")
    rows.append({**site("DC-NOR", "Example Distribution Center Norwick"), **change})
    assert unnamed(route(rows, delivery_site="DC-NOR"))


def test_company_wide_site_role_covers_known_sites_only():
    rows = seated(DEPOT, "Warehouse Chief") + [site("WH-07", "South Depot")]
    assert route(rows, delivery_site="WH-07")["also_required"][0]["name"] == "Finn Ford"
    assert unnamed(route(rows, delivery_site="Harbour Depot"))


def test_company_wide_seat_does_not_stand_in_for_an_unlinked_site_seat():
    rows = seated("North Depot (dotted", "Olu Obi\nWarehouse Chief\nNorth Depot (dotted")
    assert unnamed(route(rows + [site("WH-07", "South Depot")], delivery_site="WH-07"))


def test_name_shared_with_another_sites_id_needs_a_human():
    rows = world() + [site("WH-07", "North Depot"), site("North-Depot", "Old Store")]
    assert unnamed(route(rows, delivery_site="WH-07"))


def test_matched_site_fact_is_cited():
    fact = site("WH-07", "North Depot")
    out = route(world() + [fact], delivery_site="WH-07")
    assert out["also_required"][0]["name"] == "Finn Ford" and {"fact_id": fact["fact_id"]} in out["sources"]


def test_two_vacancies_do_not_make_one_signer_of_a_two_person_rule():
    chart = CHART_V2.replace("Eve East", "TBD").replace("Jo James", "Vacant")
    assert unnamed(route(world(POLICY, chart), action="conditional_release", amount=None))


def test_one_person_in_both_seats_is_not_two_signers():
    assert unnamed(route(seated("Eve East", "Jo James"), action="conditional_release", amount=None))


def test_escalation_to_the_other_signer_is_not_two_signers():
    policy = POLICY.replace("by the Test\nEngineer and", "by the Purchasing\nDirector and")
    assert policy != POLICY
    out = route(world(policy, CHART_V2), action="conditional_release", amount=None, category="Toys")
    assert unnamed(out)


@pytest.mark.parametrize("cell", ["700 or less", "EUR 700", "700"])
def test_limit_without_an_operator_is_reported(cell):
    assert unnamed(route(world(POLICY.replace("< 700\n", f"{cell}\n")), amount=5))
