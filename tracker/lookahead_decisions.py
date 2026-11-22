"""The decision card and the human steps on it: accept and reject.

    card(la, decision_id, stores)     what the card shows: routed signer, score drops, end state of the goal
    accept(la, decision_id, body)     {approver}: send the attached drafts, write plan.decisions, record it
    reject(la, decision_id, body)     {approver, reason}: write the reason, send nothing. Refused once mail is out

In live mode the signer comes from `sourcing.approvals.route` on kb facts, never from the record producer. The
producer gives what is routed: `actions` as `sourcing.hedge` writes them, with `category` and `delivery_site`.
A payment is routed on the larger of itself and its order, so a split cannot lower the signer. Without a
resolved single signer, counting the approvals required on top of the value, nobody can accept here.
A rehearsal has no database: it shows its script's signer, writes no plan row and never reaches mail.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime

import psycopg
from psycopg.types.json import Jsonb

from sourcing import approvals

from .lookahead import Lookahead, LookaheadError

NOT_PEOPLE = ("agent", "mail:", "system")
_one_step_at_a_time = threading.Lock()


class Stores:
    """The databases one human step needs. Settings and SMTP sender are passed in by tests."""

    def __init__(self, settings=None, sender=None):
        from mail.config import load_settings

        self.settings = settings or load_settings()
        self.sender = sender
        if not self.settings.database_url:
            raise LookaheadError(503, "no database is configured")

    def facts(self) -> list[dict]:
        from kb import db

        with db.connect(self.settings.database_url, self.settings.kb_schema) as conn:
            return conn.execute("SELECT * FROM current_facts").fetchall()

    def plan(self):
        from plan import db

        return db.connect(self.settings.database_url)

    def mail(self, actor: str):
        from mail.tools import MailTools

        return MailTools(settings=self.settings, sender=self.sender, actor=actor)


def _plain(value):
    return json.loads(json.dumps(value, default=str))


def _same_person(a: str, b: str) -> bool:
    return " ".join(a.split()).casefold() == " ".join(b.split()).casefold()


def _person(body: dict) -> str:
    name = " ".join(str(body.get("approver") or "").split())
    if not name or name.lower().startswith(NOT_PEOPLE):
        raise LookaheadError(400, "approver must name a person")
    return name


def _decision(la: Lookahead, decision_id: str) -> dict:
    decision = la.decision(decision_id)
    if not decision:
        raise LookaheadError(404, "unknown decision")
    return decision


def _open(decision: dict) -> None:
    if decision.get("status") != "open":
        raise LookaheadError(409, f"decision is {decision.get('status')}")


# ---------- signer ----------

def _route_action(decision: dict, action: dict, rows: list[dict], on: datetime) -> dict:
    amount, source = action.get("amount_eur"), action.get("amount_source")
    whole = action.get("order_value_eur")
    if action.get("action") == "purchase_order":
        if whole is None:
            amount, source = None, None  # a payment alone cannot say which limit of the order applies
        elif amount is None or whole > amount:
            amount, source = whole, {"label": "value of the whole order", "value": whole, "inputs": [source]
                                     if source else decision.get("sources", [])}
    known = {k: action.get(k, decision.get(k)) for k in ("category", "delivery_site", "budget_used_eur",
                                                         "budget_used_source", "extensions_used", "release_source")}
    return approvals.route(rows, action=action.get("action"), on=on, amount_eur=amount, amount_source=source,
                           **known)


def routed_signer(decision: dict, rows: list[dict], on: datetime) -> dict:
    """{status, signer, reasons, sources}. A name only when one person signs every action."""
    results, reasons = [], []
    actions = decision.get("actions") or []
    if not actions:
        reasons.append("the decision lists no actions to route")
    for action in actions:
        try:
            results.append(_route_action(decision, action, rows, on))
        except (ValueError, TypeError, AttributeError) as exc:
            reasons.append(f"an action cannot be routed: {exc}")
    reasons += [why for r in results for why in r["reasons"]]
    needed = [[*r["signers"], *r["also_required"]] for r in results]  # a site release comes on top of the value
    if any(len(people) > 1 for people in needed):
        reasons.append("the rule needs more than one signature, one click cannot give them")
    names = {r["signer"]["name"] for r in results if "name" in r["signer"]}
    if len(names) > 1:
        reasons.append("the actions have different signers, one click cannot sign for them all")
    resolved = (len(results) == len(actions) > 0 and len(names) == 1
                and all(r["status"] == "resolved" for r in results) and all(len(people) == 1 for people in needed))
    asked = [role for r in results for role in (r["signer"]["role"], *(s["role"] for s in r["also_required"]))]
    roles = " and ".join(dict.fromkeys(asked)) or approvals.UNRESOLVED
    signer = {"role": roles}
    cited = [s.get("rule") for r in results for s in (r["signer"], *r["also_required"])]
    if rules := list(dict.fromkeys(rule for rule in cited if rule)):
        signer["rule"] = "; ".join(rules)
    if resolved:
        signer["name"] = names.pop()
    if reasons:
        signer["reason"] = "; ".join(dict.fromkeys(reasons))
    elif resolved:
        signer["reason"] = f"{roles} holds the limit for this decision"
    sources = {json.dumps(s, sort_keys=True, default=str): s for r in results for s in r["sources"]}
    return _plain({"status": "resolved" if resolved else "needs_human", "signer": signer,
                   "reasons": list(dict.fromkeys(reasons)), "sources": list(sources.values())})


def _team_time(la: Lookahead, now: str) -> datetime:
    """`now` on the launch's own clock: the chart and the holder valid that day depend on it."""
    at = datetime.fromisoformat(now)
    try:
        zone = datetime.fromisoformat(la._latest("launch")["deadline"]).tzinfo
    except (KeyError, TypeError, ValueError):
        return at
    return at.astimezone(zone) if zone else at


# ---------- what the card shows ----------

def score_drops(state: dict, decision: dict) -> list[dict]:
    """Suppliers of this decision's lane whose latest score change was a drop."""
    lane = decision.get("lane")
    involved = {decision.get("supplier_id")}
    for row in (state.get("plan") or {}).get("lanes", []):
        if row.get("lane_id") == lane:
            involved |= {bar.get("supplier_id") for bar in row.get("bars", [])}
    involved |= {e.get("supplier_id") for e in state["events"] if e.get("lane") == lane}
    drops = []
    for supplier in state["suppliers"]:
        history = [h for h in supplier.get("history", []) if h["value"] is not None]
        if len(history) < 2 or (lane and supplier["supplier_id"] not in involved):
            continue
        before, after = history[-2], history[-1]
        if after["value"] < before["value"]:
            drops.append({"supplier_id": supplier["supplier_id"], "name": supplier.get("name"),
                          "before": before["value"], "after": after["value"], "cause": after.get("cause"),
                          "sources": supplier.get("score", {}).get("sources", [])})
    return drops


def end_state(state: dict) -> dict | None:
    """The goal bar in words. Both figures and `met` come from the latest plan record."""
    cov = (state.get("plan") or {}).get("coverage")
    if not cov:
        return None
    what = (state.get("goal") or {}).get("edition") or cov["unit"]
    return {"met": cov["met"], "covered": cov["covered"], "needed": cov["needed"],
            "text": f"{what} {cov['covered']:,} of {cov['needed']:,} covered".strip(),
            "sources": cov.get("sources", [])}


def card(la: Lookahead, decision_id: str, stores: Stores | None = None) -> dict:
    decision, state = _decision(la, decision_id), la.state()
    shown = {**decision, "score_drops": score_drops(state, decision), "end_state": end_state(state)}
    if la.mode != "live" or decision.get("status") != "open":
        return shown  # a closed decision keeps the signer it was recorded with
    on = _team_time(la, la.now())
    try:
        rows = (stores or Stores()).facts()
    except psycopg.Error as exc:
        raise LookaheadError(503, f"cannot read the approval facts: {type(exc).__name__}") from exc
    approval = routed_signer(decision, rows, on)
    return {**shown, "signer": approval["signer"], "approval": approval}


# ---------- accept and reject ----------

def accept(la: Lookahead, decision_id: str, body: dict, stores: Stores | None = None) -> dict:
    by = _person(body)
    with _one_step_at_a_time:
        decision = _decision(la, decision_id)
        _open(decision)
        now = la.now()  # before anything is sent: without a clock the decision could not be recorded
        sent: list[str] = []
        outcome = {"status": "accepted", "decided_by": by, "decided_at": now}
        if la.mode == "live":
            try:
                stores = stores or Stores()
                approval = routed_signer(decision, stores.facts(), _team_time(la, now))
                signer = approval["signer"]
                if approval["status"] != "resolved":
                    raise LookaheadError(409, f"no signer can accept this: {signer.get('reason', 'unresolved')}")
                if not _same_person(by, signer["name"]):
                    raise LookaheadError(403, f"{signer['name']}, {signer['role']} signs this decision")
                outcome.update(signer=signer, approval=approval)
                with stores.plan() as conn:
                    if earlier := _hold_row(conn, decision, outcome):
                        _all_went_out(stores, decision, by)
                        outcome["decided_at"] = earlier  # written and sent before, only the record was missing
                    else:
                        sent = _send(stores, decision, by, signer)
                        _close_row(conn, decision_id, outcome)
            except psycopg.Error as exc:
                raise LookaheadError(503, f"decision not recorded: {type(exc).__name__}") from exc
        la.append([{"kind": "decision", "decision_id": decision_id, **outcome}])
    la.after_human_step()
    return {"ok": True, "sent": sent, "end_state": end_state(la.state())}


def reject(la: Lookahead, decision_id: str, body: dict, stores: Stores | None = None) -> dict:
    by, reason = _person(body), str(body.get("reason") or "").strip()
    if not reason:
        raise LookaheadError(400, "a rejection needs a reason")
    with _one_step_at_a_time:
        decision = _decision(la, decision_id)
        _open(decision)
        outcome = {"status": "rejected", "decided_by": by, "decided_at": la.now(), "reason": reason}
        if la.mode == "live":
            try:
                stores = stores or Stores()
                approval = routed_signer(decision, stores.facts(), _team_time(la, outcome["decided_at"]))
                outcome.update(signer=approval["signer"], approval=approval)
                with stores.plan() as conn:
                    if earlier := _hold_row(conn, decision, outcome):
                        outcome["decided_at"] = earlier
                    else:
                        _none_went_out(stores, decision, by)
                        _close_row(conn, decision_id, outcome)
            except psycopg.Error as exc:
                raise LookaheadError(503, f"decision not recorded: {type(exc).__name__}") from exc
        la.append([{"kind": "decision", "decision_id": decision_id, **outcome}])
    return {"ok": True}


def _hold_row(conn, decision: dict, outcome: dict) -> str | None:
    """Lock the plan row until the step is over. Returns the time when this same step was already written."""
    key = decision["decision_id"]
    conn.execute(
        "INSERT INTO decisions (decision_id, recommendation_id, title, recommendation, lane, supplier_id, impact, "
        "cost, at, sources, options, on_accept) VALUES (%s, (SELECT recommendation_id FROM recommendations "
        "WHERE recommendation_id::text = %s), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (decision_id) DO NOTHING",
        (key, str(decision.get("recommendation_id") or ""), decision.get("title") or key,
         decision.get("recommendation"), decision.get("lane"), decision.get("supplier_id"),
         *(Jsonb(decision[k]) if decision.get(k) is not None else None for k in ("impact", "cost")),
         decision.get("at"), *(Jsonb(decision.get(k) or []) for k in ("sources", "options", "on_accept"))))
    row = conn.execute("SELECT status, title, decided_by, decided_at FROM decisions WHERE decision_id = %s "
                       "FOR UPDATE", (key,)).fetchone()
    if row["status"] == "open":
        return None
    if row["title"] != (decision.get("title") or key):
        raise LookaheadError(409, f"decision id {key} is already used by '{row['title']}', a new one needs a new id")
    if row["status"] == outcome["status"] and _same_person(row["decided_by"], outcome["decided_by"]):
        return row["decided_at"].isoformat()
    raise LookaheadError(409, f"decision is {row['status']}")


def _close_row(conn, decision_id: str, outcome: dict) -> None:
    conn.execute("UPDATE decisions SET status = %s, decided_by = %s, decided_at = %s, signer = %s, note = %s "
                 "WHERE decision_id = %s",
                 (outcome["status"], outcome["decided_by"], outcome["decided_at"], Jsonb(outcome["signer"]),
                  outcome.get("reason"), decision_id))


def _send_states(stores: Stores, decision: dict, by: str) -> dict[str, set[str]]:
    """The sends of each attached draft that went out or may have. A dry run counts only while mail is in dry_run."""
    ids = []
    for draft_id in decision.get("draft_ids") or []:
        try:
            ids.append(str(uuid.UUID(str(draft_id))))
        except ValueError:
            continue  # not a draft id, so nothing can have been sent for it
    if not ids:
        return {}
    tools = stores.mail(by)
    try:
        states = ["sending", "sent", "unknown"] + (["dry_run"] if tools.s.send_mode == "dry_run" else [])
        found: dict[str, set[str]] = {}
        for row in tools.conn.execute("SELECT draft_id::text AS draft_id, state FROM sends "
                                      "WHERE draft_id = ANY(%s::uuid[]) AND state = ANY(%s)", (ids, states)):
            found.setdefault(row["draft_id"], set()).add(row["state"])
        return found
    finally:
        tools.conn.close()


def _none_went_out(stores: Stores, decision: dict, by: str) -> None:
    if out := _send_states(stores, decision, by):
        raise LookaheadError(409, f"cannot reject, mail is out or may be out for draft {', '.join(sorted(out))}")


def _all_went_out(stores: Stores, decision: dict, by: str) -> None:
    """An accepted row answers for this step only if every draft attached now was sent."""
    out = _send_states(stores, decision, by)
    for draft_id in decision.get("draft_ids") or []:
        if not out.get(str(draft_id), set()) & {"sent", "dry_run"}:
            raise LookaheadError(409, f"decision id {decision['decision_id']} was accepted without sending draft "
                                      f"{draft_id}, a new decision needs a new id")


def _send(stores: Stores, decision: dict, by: str, signer: dict) -> list[str]:
    """Approve and send exactly the attached drafts. All are checked first, and a retry skips what went out."""
    ids = list(dict.fromkeys(str(i) for i in decision.get("draft_ids") or []))
    if not ids:
        return []
    from mail import drafts
    from mail.send import SENDABLE

    tools = stores.mail(by)
    try:
        done = {"sent", "dry_run_sent"} if tools.s.send_mode == "dry_run" else {"sent"}
        waiting = []
        for draft_id in ids:
            try:
                uuid.UUID(draft_id)
            except ValueError:
                raise LookaheadError(409, f"draft {draft_id}: not a draft id") from None
            draft = drafts.load_draft(tools.conn, draft_id)
            if not draft:
                raise LookaheadError(409, f"draft {draft_id}: draft_not_found")
            if draft["status"] in done:
                continue
            if draft["status"] not in SENDABLE:
                raise LookaheadError(409, f"draft {draft_id}: {draft['status']}")
            waiting.append(draft_id)
        for draft_id in waiting:
            try:
                ok = drafts.approve(tools.conn, draft_id, by, role=signer["role"], policy_ref=signer.get("rule"))
                tools.conn.commit()
            except drafts.DraftError as exc:
                raise LookaheadError(409, f"draft {draft_id}: {exc.code}") from exc
            result = tools.send_approved(draft_id, ok["approval_id"])
            if not result.get("ok"):
                raise LookaheadError(502, f"draft {draft_id} not sent: {result.get('error') or result}")
        return waiting
    finally:
        tools.conn.close()
