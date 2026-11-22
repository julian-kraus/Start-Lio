"""Who is in a seat and which seat is a site's. Same synthetic facts as test_approvals."""

import pytest

from approvals_world import CHART_V2, POLICY, SEATS_V2, changed, newest, seat, seat_facts, world
from test_approvals import route

DEPOT = "Warehouse Chief North Depot"
AREA = "Area Manager Paper & Board"


def site(site_id, name, path="own/sites.csv", **change):
    return {"fact_id": f"{path}|{site_id}|{name}", "path": path, "sha256": path, "doc_kind": "master_table",
            "fact_type": "site", "extractor": "test:1", "subject": {"site_id": site_id}, "attribute": "name",
            "value": name, "locator": {}, "confidence": 0.9, **change}


def unnamed(out):
    return out["status"] == "needs_human" and "name" not in out["signer"]


def at_dc():
    return newest(DEPOT, title="Warehouse Chief DC Norwick", scope="DC Norwick")


def one(attribute, /, **change):
    """The world in which one fact of the area manager's seat differs."""
    rows = world(POLICY, (CHART_V2, SEATS_V2))
    hit = [r for r in rows if r["attribute"] == attribute and r["subject"].get("title") == AREA]
    assert len(hit) == 1
    return [{**r, **change} if r is hit[0] else r for r in rows]


def test_signer_cites_the_seat_and_its_line():
    out = route()
    only, cited = out["signers"][0], {s.get("fact_id") for s in out["sources"]}
    line = CHART_V2.splitlines().index("Jo James") + 1
    assert only["line_keys"] == [f"L{line}"] and f"own/chart1.pdf|test:1|{line}" in cited
    assert len(only["seat_fact_ids"]) == 5 and set(only["seat_fact_ids"]) <= cited


def test_chart_without_seats_names_nobody_whatever_its_lines_say():
    out = route(world(POLICY, (CHART_V2, [])))
    assert unnamed(out) and out["signers"][0]["state"] == "not_in_chart"


def test_text_of_the_chart_never_changes_the_signer():
    out = route(world(POLICY, (CHART_V2.replace("Jo James", "Kim King"), SEATS_V2)))
    assert out["signer"]["name"] == "Jo James"


def test_seat_of_the_replaced_chart_never_fills_the_newer_one():
    rows = [r for r in world() if r["fact_type"] != "org_seat" or r["path"] != "own/chart1.pdf"]
    assert unnamed(route(rows))


def test_seat_of_an_older_version_of_the_file_is_not_used():
    rows = [{**r, "sha256": "older"} if r["fact_type"] == "org_seat" else r
            for r in world(POLICY, (CHART_V2, SEATS_V2))]
    assert unnamed(route(rows))


def test_seats_without_the_dating_line_could_be_the_newest_chart():
    out = route(world() + seat_facts(SEATS_V2, "own/chart9.pdf"))
    assert unnamed(out) and "chart9" in " ".join(out["reasons"])


@pytest.mark.parametrize("change", [{"doc_kind": "other_document"}, {"doc_kind": None},
                                    {"fact_type": "x_seat"}])
def test_only_org_seat_facts_of_an_org_chart_count(change):
    rows = [{**r, **change} if r["fact_type"] == "org_seat" else r for r in world(POLICY, (CHART_V2, SEATS_V2))]
    assert unnamed(route(rows))


@pytest.mark.parametrize("path, mail", [("suppliers/x/chart.pdf", None), ("mail/t/chart.pdf", None),
                                        ("own/chart0.pdf", {"trust": "supplier_contact"})])
def test_supplier_sent_seat_never_signs(path, mail):
    lines = [r for r in world(POLICY, (CHART_V2, SEATS_V2)) if r["fact_type"] != "org_seat"]
    assert unnamed(route(lines + seat_facts(SEATS_V2, path, mail=mail)))


@pytest.mark.parametrize("status", ["vacant", "absent", "unclear"])
def test_seat_that_is_not_held_never_names_its_holder(status):
    out = route(newest(AREA, status=status))
    assert unnamed(out) and out["signers"][0]["state"] == status and "name" not in out["signers"][0]
    assert "Jo James" not in " ".join(out["reasons"])
    assert out["signers"][0]["seat_fact_ids"]
    assert ("exception_approver" in out["signers"][0]) == (status != "unclear")


@pytest.mark.parametrize("change", [
    {"status": "Held"}, {"status": "active"}, {"status": ""}, {"holder": ""}, {"holder": "  "},
    {"valid_from": "April"}, {"valid_until": "2030-02-31"}, {"valid_from": "2030-03-02", "valid_until": "2030-03-01"},
])
def test_seat_that_breaks_the_contract_is_unclear(change):
    out = route(newest(AREA, **change))
    assert unnamed(out) and out["signers"][0]["state"] == "unclear"


@pytest.mark.parametrize("attribute, change", [("status", {"confidence": 0.4}), ("holder", {"confidence": 0.4}),
                                               ("holder", {"value": ["Jo James"]}), ("holder", {"value": None}),
                                               ("status", {"value": None}), ("status", {"attribute": "state"})])
def test_seat_fact_that_cannot_be_trusted_names_nobody(attribute, change):
    assert unnamed(route(one(attribute, **change)))


@pytest.mark.parametrize("extractor", ["test:1", "llm:test"])
@pytest.mark.parametrize("change", [{"holder": "Kim King"}, {"status": "absent"}, {"valid_until": "2030-03-01"}])
def test_two_readings_of_a_seat_that_disagree_need_a_human(extractor, change):
    other = seat_facts(changed(SEATS_V2, AREA, **change), "own/chart0.pdf", extractor=extractor)
    assert unnamed(route(world(POLICY, (CHART_V2, SEATS_V2)) + other))


def test_two_equal_readings_of_a_seat_are_one_holder():
    again = seat_facts(SEATS_V2, "own/chart0.pdf", extractor="llm:test")
    assert route(world(POLICY, (CHART_V2, SEATS_V2)) + again)["signer"]["name"] == "Jo James"


@pytest.mark.parametrize("title, scope", [("Area Manager", "Paper & Board"), (AREA, ""),
                                          ("Area Manager (Paper & Board)", "Paper & Board")])
def test_scope_comes_from_the_key_or_the_title(title, scope):
    rows = newest(AREA, title=title, scope=scope)
    assert route(rows)["signer"]["name"] == "Jo James"
    assert "Paper & Board" in route(rows)["signer"]["role"]
    assert unnamed(route(rows, category="Glass"))


@pytest.mark.parametrize("title, scope", [(AREA, "Toys"), ("Area Manager Assistant", "Paper & Board")])
def test_title_and_scope_that_differ_do_not_widen_the_seat(title, scope):
    assert unnamed(route(newest(AREA, title=title, scope=scope)))


@pytest.mark.parametrize("holder", ["Dr. Ben Baker", "Anna van der Berg", "Liam O'Brien", "Sean McAdam",
                                    "J. Porter", "Zoë Østby", "Suharto", "JO JAMES", "王 小明"])
def test_held_seat_signs_whatever_the_shape_of_the_name(holder):
    out = route(newest(AREA, holder=holder))
    assert out["status"] == "resolved" and out["signer"]["name"] == holder


@pytest.mark.parametrize("wanted", ["North Depot Annex", "Depot", "North", "South Depot near North Depot Road"])
@pytest.mark.parametrize("in_kb", [False, True])
def test_another_site_never_gets_this_sites_holder(wanted, in_kb):
    rows = world() + ([site("SD-ANX", wanted)] if in_kb else [])
    assert unnamed(route(rows, delivery_site=wanted))


@pytest.mark.parametrize("wanted", ["DC", "Norwick", "DC-NOR", "Example Northern Distribution Center Norwick"])
def test_part_of_a_name_or_an_id_without_site_facts_needs_a_human(wanted):
    assert unnamed(route(at_dc(), delivery_site=wanted))


@pytest.mark.parametrize("site_id, name", [("DC-NOR", "Example Norwick Distribution Center Annex"),
                                           ("WH-07", "Example Northern Distribution Center Norwick")])
def test_site_that_only_resembles_the_seat_needs_a_human(site_id, name):
    rows = at_dc() + [site(site_id, name)]
    assert unnamed(route(rows, delivery_site=site_id))


def test_seat_that_fits_two_sites_needs_a_human():
    rows = at_dc() + [site("DC-NOR", "Example Distribution Center Norwick"), site("DC-NRW", "DC Norwick")]
    assert unnamed(route(rows, delivery_site="DC-NOR"))


@pytest.mark.parametrize("change", [{"path": "suppliers/x/sites.csv"}, {"path": "mail/t/sites.csv"},
                                    {"locator": {"mail": {"trust": "supplier_contact"}}}, {"confidence": 0.4}])
def test_untrusted_site_fact_links_no_holder(change):
    rows = at_dc()
    rows.append({**site("DC-NOR", "Example Distribution Center Norwick"), **change})
    assert unnamed(route(rows, delivery_site="DC-NOR"))


def test_company_wide_site_role_covers_known_sites_only():
    rows = newest(DEPOT, title="Warehouse Chief", scope="") + [site("WH-07", "South Depot")]
    assert route(rows, delivery_site="WH-07")["also_required"][0]["name"] == "Finn Ford"
    assert unnamed(route(rows, delivery_site="Harbour Depot"))


def test_company_wide_seat_does_not_stand_in_for_an_unlinked_site_seat():
    rows = world(POLICY, (CHART_V2, SEATS_V2 + [seat("Warehouse Chief", "Olu Obi")]))
    assert unnamed(route(rows + [site("WH-07", "South Depot")], delivery_site="WH-07"))


def test_name_shared_with_another_sites_id_needs_a_human():
    rows = world() + [site("WH-07", "North Depot"), site("North-Depot", "Old Store")]
    assert unnamed(route(rows, delivery_site="WH-07"))


def test_matched_site_fact_is_cited():
    fact = site("WH-07", "North Depot")
    out = route(world() + [fact], delivery_site="WH-07")
    assert out["also_required"][0]["name"] == "Finn Ford" and {"fact_id": fact["fact_id"]} in out["sources"]


def test_two_vacancies_do_not_make_one_signer_of_a_two_person_rule():
    seats = changed(changed(SEATS_V2, "Test Engineer", status="vacant"), AREA, status="vacant")
    assert unnamed(route(world(POLICY, (CHART_V2, seats)), action="conditional_release", amount=None))


def test_one_person_in_both_seats_is_not_two_signers():
    assert unnamed(route(newest("Test Engineer", holder="Jo James"), action="conditional_release", amount=None))


def test_exception_approver_who_is_the_other_signer_is_not_two_signers():
    policy = POLICY.replace("by the Test\nEngineer and", "by the Purchasing\nDirector and")
    assert policy != POLICY
    out = route(world(policy), action="conditional_release", amount=None, category="Toys")
    assert unnamed(out)


@pytest.mark.parametrize("cell", ["700 or less", "EUR 700", "700"])
def test_limit_without_an_operator_is_reported(cell):
    assert unnamed(route(world(POLICY.replace("< 700\n", f"{cell}\n")), amount=5))
