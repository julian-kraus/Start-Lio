"""Readings that must never end in a named signer. Same synthetic facts as test_approvals."""

import pytest

from test_approvals import CHART_V1, POLICY, facts, route, world

UNDATED = POLICY.replace(" · Effective 2030-01-01", "")
NEWER = POLICY.replace("v2.0", "v3.0").replace("Version 2.0", "Version 3.0").replace("2030-01-01", "2030-02-01")


def with_holder(holder):
    return world(POLICY, CHART_V1.replace("Cleo Cole", holder))


def second_policy(text, scope="AP-1", path="own/policy3.pdf"):
    return world() + facts(text, path, "policy_document", scope=scope)


@pytest.mark.parametrize("holder", ["To be appointed", "To Be Announced", "Not yet appointed", "Vacancy",
                                    "Vacant position", "Position vacant", "Open role", "Unassigned", "Unfilled",
                                    "N.N", "TBD."])
def test_vacancy_wordings_escalate_like_a_vacancy(holder):
    out = route(with_holder(holder))
    assert out["status"] == "escalated"
    assert out["signer"]["name"] == "Dr. Ben Baker"


@pytest.mark.parametrize("holder", ["Interim", "Recruiting", "None", "Nobody", "Pending", "TBD pending",
                                    "Acting Manager", "Position under review"])
def test_placeholder_that_is_not_clearly_a_vacancy_needs_a_human(holder):
    out = route(with_holder(holder))
    assert out["status"] == "needs_human"
    assert "name" not in out["signer"]


@pytest.mark.parametrize("holder", ["Minh To", "Tobe Nwigwe", "Nona Vance", "Pia Openshaw"])
def test_names_that_resemble_placeholders_still_sign(holder):
    out = route(with_holder(holder))
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
    out = route(rows + facts(CHART_V1, "own/chart.pdf", "org_chart"))
    assert out["status"] == "resolved"
    assert out["policy"]["version"] == "3.0"
