"""Routing on the reference facts in structured/docs. Every expectation is derived from the facts, none is typed."""

import json
from datetime import timedelta
from pathlib import Path

import pytest

from sourcing import approval_docs, approvals

DOCS = Path(__file__).resolve().parent.parent / "structured" / "docs"
KINDS = (approval_docs.POLICY_KIND, approval_docs.CHART_KIND)


@pytest.fixture(scope="module")
def reference():
    rows = []
    for path in sorted(DOCS.glob("*.json")):
        env = json.loads(path.read_text())
        if env.get("doc_kind") not in KINDS:
            continue
        for i, fact in enumerate(env["facts"]):
            rows.append({**fact, "fact_id": f"{path.stem}:{i}", "path": env["source"]["path"],
                         "sha256": env["source"]["sha256"], "doc_kind": env["doc_kind"], "confidence": 0.9})
    if not rows:
        pytest.skip("no reference policy or org chart")
    policy = approval_docs.read_policy(approval_docs.documents(rows, KINDS[0])[0])
    charts = sorted((approval_docs.read_chart(d, policy.role_terms())
                     for d in approval_docs.documents(rows, KINDS[1])), key=lambda c: c.dated)
    return rows, policy, charts


def test_reference_policy_is_read_completely(reference):
    _, policy, charts = reference
    assert not policy.problems and policy.tiers and policy.currency
    assert policy.air.limit and policy.air.roles and policy.air.above
    assert policy.release.roles and policy.release.days and policy.release.extension.roles
    assert policy.site.roles and len(policy.exceptions.roles) == 1
    assert not any(c.problems or any(s.state == "unclear" for s in c.seats) for c in charts)


def test_reference_every_tier_and_category_has_a_signer(reference):
    rows, policy, charts = reference
    newest = charts[-1]
    areas = {scope for s in newest.seats for role in policy.tiers[1].roles
             if (scope := approval_docs.title_scope(s.title, role))}
    assert areas
    for area in areas:
        for i, tier in enumerate(policy.tiers[1:], 1):
            amount = policy.tiers[i - 1].amount
            out = approvals.route(rows, action="purchase_order", on=newest.dated, amount_eur=amount,
                                  amount_source="reference test", category=area)
            assert out["status"] in ("resolved", "escalated"), out["reasons"]
            assert [s["required_role"] for s in out["signers"]] == tier.roles
            assert out["org_chart"]["version"] == newest.version


def test_reference_future_holder_escalates_until_the_start(reference):
    rows, policy, charts = reference
    for seat in charts[-1].seats:
        if not seat.start:
            continue
        i, tier, area = next((i, t, scope) for i, t in enumerate(policy.tiers) for role in t.roles
                             if (scope := approval_docs.title_scope(seat.title, role)))

        def signed(on):
            return approvals.route(rows, action="purchase_order", on=on, amount_source="reference test",
                                   amount_eur=policy.tiers[i - 1].amount if i else 0, category=area)

        assert signed(seat.start - timedelta(days=1))["status"] == "escalated"
        assert signed(seat.start)["signer"]["name"] == seat.name
