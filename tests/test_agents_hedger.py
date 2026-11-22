"""The hedger on synthetic facts: the math, the guard, the replayed debate and what is written to plan."""

import json
import os
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from psycopg import sql

from agents import hedger, llm
from plan import contracts
from sourcing import timing, weakspots
from sourcing.facts import Facts
from sourcing.hedge import Input, ModelError

from approvals_world import world as approval_world
from sourcing_world import NOW, World

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "replay"
URL = os.environ.get("TEST_DATABASE_URL")
TERMS = {"scenario_input": "backup terms"}
OFFER_REF = {"scenario_input": "backup offer"}
GOAL = {"statement": "Every Deluxe copy on time.", "unit_loss_eur": 100}
NEEDED = NOW + timedelta(hours=12)


@pytest.fixture
def spots():
    return ranked()


def ranked(unreleased=None, part="BOOK"):
    """The weak spots, with S4 listed for the part under that qualification status where a test gives one."""
    w = World()
    w.add("plan_edition", {"product_id": "P-A"}, preorders=1100)
    w.add("plan_edition", {"product_id": "P-B"}, preorders=1100)
    w.drop("stock_batch", material_id="BOOK")
    w.stock("BOOK", 1400)
    for material, suppliers in (("CARD", ("S1", "S2")), ("BOOK", ("S3",)), ("CASE", ("S1", "S2"))):
        for supplier in suppliers:
            w.add("supplier_part", {"material_id": material, "supplier_id": supplier}, primary="Y")
    for supplier in ("S1", "S2", "S3"):
        w.add("supplier", {"supplier_id": supplier}, status="active", qualification_status="qualified")
    if unreleased:
        w.add("supplier_part", {"material_id": part, "supplier_id": "S4"}, primary="N")
        w.add("supplier", {"supplier_id": "S4"}, status="active", qualification_status=unreleased)
    return weakspots.rank(Facts(w.current, w.believed, w.issues), w.run(), GOAL)


def backup(offer_id="OFF-1", supplier="S9", price=4, deposit="25%", **offer):
    legs = {name: timing.Leg(name, hours, {"scenario_input": f"{name} time"})
            for name, hours in (("load", 1), ("transit", 2), ("last_mile", 1), ("receiving", 0.5))}
    terms = {"offer_id": offer_id, "supplier_id": supplier, "part_id": "BOOK", "quantity": 600,
             "unit_price_eur": price, "sources": [OFFER_REF], **offer}
    return hedger.Backup(terms, timing.Chain(**legs), NEEDED, {"scenario_input": "needed at"},
                         units_per_copy=Input(2, {"fact_id": "bom-1"}), cost_model=Input("deposit_only", TERMS),
                         deposit=Input(deposit, TERMS), category="Paper & Board")


def card(keeps=0.7):
    return {"supplier_id": "S3", "score": {"value": keeps, "history": {"n": 10}, "sources": [{"fact_id": "po-1"}]}}


def book(spots):
    return next(s for s in spots if s["part_id"] == "BOOK")


def case(spots, offer=None, keeps=0.7, goal=GOAL, now=NOW):
    return hedger.build_case(book(spots), offer or backup(), primary=card(keeps), goal=goal,
                             approval_facts=approval_world(), now=now)


def replay(name):
    return llm.ReplayClient(FIXTURES / f"{name}.json")


def scripted(*answers):
    return llm.ReplayClient([{"stop_reason": "tool_use", "tool_calls": [{"name": hedger.TOOL, "input": {}}]},
                             *({"text": json.dumps(a)} for a in answers)])


def test_the_math_comes_from_sourcing_with_its_sources(spots):
    c = case(spots)
    m = c.math
    assert (m["copies_at_risk"], m["loss_per_copy_eur"], m["commitment_eur"]) == (300, 100, 600)
    assert m["expected_loss_eur"] == pytest.approx(9000) and m["verdict"] == "hedge"
    assert all(row["refs"] for row in m["drilldown"]["inputs"])


def test_signer_follows_the_whole_order_and_not_the_deposit(spots):
    c = case(spots)
    assert c.signer["status"] == "resolved" and c.signer["amount"]["value"] == 2400
    assert c.signer["signer"]["name"] == "Cleo Cole"        # the deposit alone would stop at the planner


def test_decide_by_is_the_first_of_lead_time_expiry_and_team_window(spots):
    by = lambda c: datetime.fromisoformat(c.decide_by)
    assert by(case(spots)) == NEEDED - timedelta(hours=4.5)
    soon = case(spots, backup(valid_until=NOW + timedelta(hours=3)))
    assert by(soon) == NOW + timedelta(hours=3) and "expires" in soon.decide_by_why
    away = case(spots, goal={**GOAL, "team_window": (NOW + timedelta(hours=2)).isoformat()})
    assert by(away) == NOW + timedelta(hours=2) and "team" in away.decide_by_why


@pytest.mark.parametrize("offer, message", [
    ({"fixed_cost_eur": 5}, "fixed cost"), ({"sources": []}, "source"), ({"quantity": None}, "quantity"),
    ({"min_quantity": 900}, "minimum"), ({"valid_until": NOW - timedelta(hours=1)}, "too late"),
])
def test_an_offer_that_cannot_be_judged_is_refused(spots, offer, message):
    with pytest.raises(ModelError, match=message):
        case(spots, backup(**offer))


def test_database_numerics_are_accepted(spots):
    c = case(spots, backup(price=Decimal("4.00"), quantity=Decimal("600")),
             goal={**GOAL, "unit_loss_eur": Decimal("100")})
    assert c.math["order_value_eur"] == 2400


def test_one_part_gets_the_offer_worth_most_for_its_cost(spots):
    offers = [backup("DEAR", price=9), backup("CHEAP", price=3), backup("OWN", supplier="S3", price=1),
              backup("GONE", price=1, status="expired"), backup("LATE", valid_until=NOW - timedelta(hours=1))]
    best, problems = hedger.choose(book(spots), offers, primary=card(), goal=GOAL, now=NOW,
                                   approval_facts=approval_world())
    assert best.backup.offer["offer_id"] == "CHEAP"
    assert len(problems) == 1 and problems[0].startswith("LATE")


@pytest.mark.parametrize("status", ["conditional", "expired", "not qualified"])
def test_a_backup_that_is_not_qualified_gets_no_signer(status):
    spots = ranked(status)
    assert book(spots)["detail"]["not_qualified"] == ["S4"]
    with pytest.raises(ModelError, match="S4 is not qualified on file"):
        case(spots, backup(supplier="S4", price=1))


def test_a_qualified_offer_wins_over_a_cheaper_one_that_is_not():
    offers = [backup("UNRELEASED", supplier="S4", price=1), backup("QUALIFIED", price=2)]
    best, problems = hedger.choose(book(ranked("conditional")), offers, primary=card(), goal=GOAL, now=NOW,
                                   approval_facts=approval_world())
    assert best.backup.offer["offer_id"] == "QUALIFIED"
    assert len(problems) == 1 and problems[0].startswith("UNRELEASED") and "not qualified" in problems[0]


def test_replayed_debate_renders_tool_figures(spots):
    out = hedger.debate(replay("hedger_debate"), case(spots))
    assert out["decision"] == out["model_verdict"] == "hedge" and out["failures"] == []
    assert "9,000.00 EUR" in out["prosecutor"] and "30.0%" in out["defender"]
    assert "{" not in out["summary"]


def test_no_role_sees_a_figure_before_it_calls_the_tool(spots):
    sent = []

    class Spy(llm.ReplayClient):
        def message(self, **request):
            if len(request["messages"]) == 1:
                sent.append(request)
            return super().message(**request)

    hedger.debate(Spy(FIXTURES / "hedger_debate.json"), case(spots))
    assert len(sent) == 3
    for request in sent:
        first = request["system"] + request["messages"][0]["content"]
        assert not any(c.isdigit() for c in first)
        assert [t["input_schema"]["properties"] for t in request["tools"]] == [{}]
        assert request["schema"]["additionalProperties"] is False


@pytest.mark.parametrize("argument", [
    "We lose 9000 EUR.", "We lose nine thousand euros.", "A quarter is ½ of it.", "It costs {invented}.", "",
    "It costs {commitment_eur", "The risk is thirty percent.",
])
def test_a_typed_or_invented_figure_is_rejected(spots, argument):
    c = case(spots)
    assert hedger.RoleGuard(c, "defender").check({"argument": argument}, [c.tool_result()])


def test_an_answer_without_the_tool_call_is_rejected(spots):
    assert hedger.RoleGuard(case(spots), "defender").check({"argument": "Wait."}, [])
    assert hedger.RoleGuard(case(spots), "defender").check({"argument": "Wait."}, [{"error": "no tool"}])


@pytest.mark.parametrize("ruling", [
    {}, {"disputed_input": "p_fail"}, {"dispute": "It is stale."}, {"disputed_input": "mood", "dispute": "Stale."},
])
def test_a_different_decision_needs_the_disputed_input_and_why(spots, ruling):
    c = case(spots)
    assert hedger.RoleGuard(c, "judge").check({"decision": "hold", "reason": "Wait.", **ruling}, [c.tool_result()])


def test_undisputed_difference_loses_to_the_deterministic_verdict(spots):
    out = hedger.debate(replay("hedger_stubborn"), case(spots))
    assert out["decision"] == "hedge" and out["disputed"] is None
    assert len(out["failures"]) == 1 and "judge" in out["failures"][0]


def test_a_named_dispute_changes_the_decision_and_pays_nothing(spots):
    c = case(spots)
    out = hedger.debate(replay("hedger_dispute"), c)
    assert out["decision"] == "hold" and out["disputed"]["name"] == "p_fail"
    rec = hedger.recommend(c, out, "run-1")
    assert rec["kind"] == "hold" and rec["actions"] == []
    assert "disputes" in rec["rationale"] and "The hedge rule says hedge" in rec["rationale"]


def test_a_disputed_hedge_against_a_hold_pays_nothing(spots):
    c = case(spots, keeps=0.99)
    assert c.math["verdict"] == "hold"
    side = [{"stop_reason": "tool_use", "tool_calls": [{"name": hedger.TOOL}]}, {"text": '{"argument": "Wait."}'}]
    judge = [side[0], {"text": json.dumps({"decision": "hedge", "reason": "Act.", "disputed_input": "p_fail",
                                           "dispute": "The score is stale."})}]
    rec = hedger.recommend(c, hedger.debate(llm.ReplayClient(side + side + judge), c), "run-1")
    assert rec["kind"] == "hedge" and rec["actions"] == []


@pytest.mark.parametrize("client", [llm.ReplayClient([]), scripted("not an object")])
def test_a_failed_model_leaves_the_deterministic_result(spots, client):
    out = hedger.debate(client, case(spots))
    assert out["decision"] == "hedge" and len(out["failures"]) == 3


def test_a_provider_error_leaves_the_deterministic_result(spots):
    class Down(llm.LLMClient):
        def message(self, **_):
            raise RuntimeError("overloaded")

    out = hedger.debate(Down(), case(spots, keeps=0.99))
    assert out["decision"] == "hold" and len(out["failures"]) == 3


@pytest.fixture
def conn(monkeypatch):
    if not URL:
        pytest.skip("TEST_DATABASE_URL not set")
    from plan import clock, db

    monkeypatch.delenv(clock.ENV_VAR, raising=False)
    schema = f"plan_test_{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("PLAN_SCHEMA", schema)
    c = db.connect(URL, schema)
    db.migrate(c, schema)
    c.commit()
    yield c
    c.rollback()
    c.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
    c.commit()
    c.close()


def hedge_parts(conn, client, spots, *offers):
    return hedger.hedge_parts(conn, client, spots, offers, primaries={"BOOK": card()}, goal=GOAL,
                              approval_facts=approval_world(), now=NOW)


def test_writes_the_recommendation_with_everything_the_card_needs(conn, spots):
    out = hedge_parts(conn, replay("hedger_debate"), spots, backup())
    assert out["skipped"] == [] and len(out["recommendations"]) == 1
    rec = out["recommendations"][0]
    contracts.validate("recommendation", rec)
    row = conn.execute("SELECT * FROM recommendations").fetchone()
    assert str(row["recommendation_id"]) == rec["recommendation_id"]
    assert row["kind"] == "hedge" and row["p_fail"] == pytest.approx(0.3)
    assert (row["loss_per_copy_eur"], row["commitment_eur"]) == (100, 600)
    assert row["decide_by"] == NEEDED - timedelta(hours=4.5) and row["decide_by_why"]
    stored = row["hedge_inputs"]
    assert stored["signer"]["name"] == "Cleo Cole" and stored["goal_line"].startswith(GOAL["statement"])
    assert "Judge, hedge" in stored["debate"]["summary"]
    assert [a["amount_eur"] for a in stored["actions"]] == [600, 1800]

    runs = conn.execute("SELECT * FROM runs").fetchall()
    assert len(runs) == 1 and runs[0]["status"] == "done"
    traces = conn.execute("SELECT * FROM agent_traces").fetchall()
    assert {t["role"] for t in traces} == {"prosecutor", "defender", "judge"}
    assert sum(t["tool"] == hedger.TOOL for t in traces) == 3


def test_a_rerun_supersedes_the_open_proposal_only(conn, spots):
    first = hedge_parts(conn, replay("hedger_debate"), spots, backup())["recommendations"][0]
    second = hedge_parts(conn, replay("hedger_debate"), spots, backup())["recommendations"][0]
    states = {str(r["recommendation_id"]): r["status"] for r in conn.execute("SELECT * FROM recommendations")}
    assert states == {first["recommendation_id"]: "superseded", second["recommendation_id"]: "proposed"}
    conn.execute("UPDATE recommendations SET status = 'decided' WHERE status = 'proposed'")
    hedge_parts(conn, replay("hedger_debate"), spots, backup())
    assert sorted(r["status"] for r in conn.execute("SELECT status FROM recommendations")) == [
        "decided", "proposed", "superseded"]


def test_a_model_failure_is_recorded_in_the_trace(conn, spots):
    rec = hedge_parts(conn, llm.ReplayClient([]), spots, backup())["recommendations"][0]
    assert rec["kind"] == "hedge" and len(rec["debate"]["failures"]) == 3
    assert conn.execute("SELECT count(*) AS n FROM agent_traces WHERE verdict_ok = false").fetchone()["n"] == 3
    assert conn.execute("SELECT error FROM runs").fetchone()["error"]


def test_only_critical_parts_with_an_offer_are_debated(conn, spots):
    assert {s["part_id"] for s in spots if s["detail"]["status"] == "critical"} == {"BOOK"}
    out = hedge_parts(conn, llm.ReplayClient([]), spots)
    assert out == {"recommendations": [], "skipped": [{"part_id": "BOOK", "reasons": ["no offer"]}]}
    out = hedge_parts(conn, llm.ReplayClient([]), spots, backup(part_id="CARD"))
    assert out["recommendations"] == []
    out = hedge_parts(conn, llm.ReplayClient([]), spots, backup(valid_until=NOW - timedelta(hours=1)))
    assert "too late" in out["skipped"][0]["reasons"][0]
    assert conn.execute("SELECT count(*) AS n FROM runs").fetchone()["n"] == 0


@pytest.mark.parametrize("listed_for", ["BOOK", "CARD"])      # qualification belongs to the supplier, not the part
def test_an_unqualified_backup_alone_is_skipped_with_the_reason_and_pays_nothing(conn, listed_for):
    spots = iter(ranked("conditional", listed_for))
    out = hedge_parts(conn, llm.ReplayClient([]), spots, backup(supplier="S4", price=1))
    assert out["recommendations"] == [] and "not qualified" in out["skipped"][0]["reasons"][0]
    for table in ("recommendations", "offers", "runs"):
        assert conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"] == 0
