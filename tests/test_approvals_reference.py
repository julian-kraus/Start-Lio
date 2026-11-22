"""Routing on the reference facts in structured/docs. Every expectation is derived from the facts, none is typed."""

import json
from pathlib import Path

import pytest

from sourcing import approval_chart, approval_docs, approvals

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
    lines = [r for r in rows if r["fact_type"] != approval_chart.SEAT_TYPE]
    charts, _ = approval_chart.read_charts(lines, approval_docs.documents(lines, KINDS[1]))
    return lines, policy, sorted(charts, key=lambda c: c.dated)


def order(rows, policy, chart, tier):
    amount = policy.tiers[tier - 1].amount if tier else 0
    return approvals.route(rows, action="purchase_order", on=chart.dated, amount_eur=amount,
                           amount_source="reference test", category="Synthetic area")


def test_reference_policy_is_read_completely(reference):
    _, policy, charts = reference
    assert not policy.problems and policy.tiers and policy.currency
    assert policy.air.limit and policy.air.roles and policy.air.above
    assert policy.release.roles and policy.release.days and policy.release.extension.roles
    assert policy.site.roles and len(policy.exceptions.roles) == 1
    assert charts and not any(c.problems for c in charts)


def test_reference_lines_alone_name_nobody(reference):
    rows, policy, charts = reference
    for tier in range(len(policy.tiers)):
        out = order(rows, policy, charts[-1], tier)
        assert out["status"] == "needs_human" and "name" not in out["signer"]


def test_reference_every_tier_signs_once_its_seats_are_held(reference):
    rows, policy, charts = reference
    newest = charts[-1]
    roles = list(dict.fromkeys(role for tier in policy.tiers for role in tier.roles))
    # one seat per title: an abbreviated role is the title written out, where the policy has it
    titles = {}
    for role in roles:
        same = [r for r in roles if r != role and approval_chart.title_scope(r, role) == ""]
        short = len(role) == 3 and role.isupper()
        titles[role] = same[0] if short and same else f"Chief {role[1]}x Officer" if short else role
    seats = [{"fact_id": f"seat{n}:{name}", "path": newest.path, "sha256": newest.sha256, "doc_kind": KINDS[1],
              "fact_type": approval_chart.SEAT_TYPE, "subject": {"title": title, "scope": ""}, "attribute": name,
              "value": value, "confidence": 0.9}
             for n, title in enumerate(dict.fromkeys(titles.values())) for name, value in (("holder", f"Person {n}"), ("status", "held"))]
    for i, tier in enumerate(policy.tiers):
        out = order(rows + seats, policy, newest, i)
        assert out["status"] == "resolved", out["reasons"]
        assert [s["required_role"] for s in out["signers"]] == tier.roles
        assert out["org_chart"]["version"] == newest.version
