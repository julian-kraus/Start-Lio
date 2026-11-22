"""The art-book scenario as a Lookahead record script, computed from kb and sourcing/, never typed.

    records = build(World.load(conn), yaml.safe_load(open("projector/scenarios/artbook.yaml")))

Numbers come from the world (kb facts through sourcing/) or from the scenario file, which only holds what the
scenario adds: the outside offer, the backup lot and its deposit, the cancellation, the beat times and the goal
answers the presenter gives. Every scenario value is labelled as one on screen.
"""
from __future__ import annotations

from sourcing import hedge, weakspots
from sourcing.facts import number

from .world import World, goal_of, instant

SCN = "scenario input"


def eur(x: float) -> str:
    return f"€{x:,.0f}" if abs(x - round(x)) < 0.005 else f"€{x:,.2f}"


def num(x: float) -> str:
    return f"{x:,.0f}"


def val(x):
    return x.get("value") if isinstance(x, dict) else x


def ids(x) -> list[dict]:
    fids = x.get("fact_ids", []) if isinstance(x, dict) else []
    return [{"fact_id": f} for f in fids[:20]] or [{"scenario_input": "computed"}]


MODELS = {"deposit_only": "deposit only, the backup can be cancelled", "full_order": "full order"}


def shown(row: dict) -> str:
    """A hedge drill-down value as the screen shows it, with its unit."""
    v, unit = row.get("value"), str(row.get("unit") or "")
    if isinstance(v, str):
        return MODELS.get(v, v)
    if "eur" in unit.casefold() or "price" in str(row.get("name", "")) or "deposit" in str(row.get("name", "")):
        return eur(v)
    if isinstance(v, float) and v < 1 and "p_fail" in str(row.get("name", "")):
        return f"{v:.0%}"
    return num(v) if isinstance(v, (int, float)) else str(v)


def supplier_record(w: World, card: dict, master: dict, sid: str, parts: list[str], **extra) -> dict:
    return {"kind": "supplier", "supplier_id": sid, "name": master.get("name") or card.get("name") or sid,
            "country": master.get("country"), "city": master.get("city"), "lang": master.get("preferred_language"),
            "contact": master.get("contact_name"), "persona": master.get("notes") or "", "parts": parts,
            "flags": [], "score": {k: card["score"][k] for k in ("value", "n", "prior", "k", "method", "breakdown")},
            **extra}


def lanes(w: World, cov: dict, spots: list[dict], edition: dict, extra_copies: dict | None = None) -> list[dict]:
    """One lane per part of the edition, in weak spot order. Gaps are copies of the edition, as weakspots prices
    them against the goal; `extra_copies` are copies a backup now covers for a part."""
    open_ = val(edition["open"])
    out = []
    for s in spots:
        pid = s["part_id"]
        # only copies the goal puts a price on count: a part whose shortage a substitute covers, or that only
        # another edition lacks, has no money at risk under this goal
        at_risk = (val(s["detail"].get("copies_at_risk")) or 0) if s.get("exposure_eur") else 0
        added = (extra_copies or {}).get(pid, 0)
        gap = max(0, at_risk - added)
        status = ("covered" if added else "ok") if gap == 0 else (s["detail"].get("status") or "critical")
        out.append({"lane_id": pid, "name": w.part_name(pid), "edition": edition["name"], "need": open_,
                    "usable": open_ - gap, "gap": gap, "status": status, "reason": s.get("reason") or "",
                    "rank": s.get("rank"), "bars": []})
    return out


def plan(at: str, lane_list: list[dict], covered: float, needed: float) -> dict:
    return {"kind": "plan", "at": at, "lanes": lane_list,
            "coverage": {"covered": covered, "needed": needed, "unit": "copies"}}


def build(w: World, scn: dict) -> list[dict]:
    goal, start = goal_of(scn), instant(scn["start"])
    cov = w.coverage(start)
    edition = w.product(scn["edition"], cov)
    spots = w.spots(cov, goal)
    top = spots[0]
    part = top["part_id"]
    at_risk = val(top["detail"]["copies_at_risk"])
    exposure = top["exposure_eur"]
    masters, site = w.suppliers(), w.site()
    off, back, canc = dict(scn["offer"]), dict(scn["backup"]), scn["cancellation"]
    # the scenario names roles, never supplier ids: both are resolved from kb here
    if back["supplier_id"] == "qualified_source":
        sources = weakspots.qualified_sources(w.facts, part)[0]
        if not sources:
            raise ValueError(f"no qualified source for {part} in kb")
        back["supplier_id"] = sources[0]
    if off["history_from"] == "least_reliable":
        cards = [w.card(s) for s in w.history.suppliers]
        cards = [c for c in cards if c["score"]["n"] >= 10]
        off["history_from"] = min(cards, key=lambda c: c["score"]["value"])["supplier_id"]
    b_id = back["supplier_id"]
    b_name = masters.get(b_id, {}).get("name", b_id)
    demand, open_, covered = val(edition["demand"]), val(edition["open"]), val(edition["covered"])

    # Supplier A: an outside offer whose score is computed from a real supplier's history (decision on #43).
    a_card = w.card(off["history_from"])
    a_src = masters.get(off["history_from"], {}).get("name", off["history_from"])
    a_master = {"name": off["label"], "city": off["city"], "country": off["country"],
                "preferred_language": off["lang"], "contact_name": "Offer desk",
                "notes": f"Scenario supplier. Its score is computed from {a_src}'s real order history ({SCN})."}

    price = w.price(b_id, part, start)
    qty = [r for r in w.facts.rows("bom_line") if str(r.get("child_id") or r.get("component_id")) == part
           and str(r.get("parent_id") or r.get("product_id")) == edition["product_id"]]
    per_copy = number(qty[0].get("qty_per")) if qty and qty[0].get("qty_per") not in (None, "") else 1
    result = hedge.decide(
        p_fail=hedge.p_fail(a_card), loss_per_copy=hedge.goal_loss(goal, edition["name"]),
        copies_at_risk=hedge.Input(at_risk, ids(top["detail"]["copies_at_risk"]), computed=True),
        units=hedge.Input(back["units"], {"scenario_input": f"{b_name} spare lot of {num(back['units'])}"}),
        unit_price=price, units_per_copy=hedge.Input(per_copy, {"scenario_input": "BOM quantity per copy"}),
        cost_model=hedge.Input("deposit_only", {"scenario_input": "backup cancellable, deposit only"}),
        deposit=hedge.Input(back["deposit"], {"scenario_input": f"deposit {back['deposit']}"}), goal=goal)
    deposit = result["commitment_eur"]
    order_value = back["units"] * val(price.value if hasattr(price, "value") else price)
    remaining = order_value - deposit
    unit_price = order_value / back["units"]
    signer = w.signer(remaining, {"label": f"remaining order value {eur(remaining)}"}, instant(scn["decision_at"]),
                      None, site.get("site_id"))
    rule = signer.get("rule") or "; ".join(signer.get("reasons", [])[:1])
    if not signer.get("name") and str(signer.get("role", "")).casefold() == "buyer":
        # the policy names the role; the org chart's line order is unknown, so the person is the buyer who
        # places this supplier's orders for this part in the PO history
        name, fact_ids = w.buyer_of(b_id, part)
        if name:
            signer["name"] = name
            rule = f"{rule}. {name} is the buyer on {b_name}'s orders for this part (PO history)"
            signer["fact_ids"] = fact_ids

    recs: list[dict] = []
    add = recs.append
    add({"kind": "launch", "name": edition["name"], "product": edition["product_id"], "mode": "rehearse",
         "deadline": w.deadline.isoformat(), "site": site.get("site_id"), "timezone": "Europe/Berlin",
         "pack_windows": [{"from": x.start.isoformat(), "to": x.end.isoformat()} for x in w.windows],
         "goal_questions": [
             {"id": "goal", "q": "What has to be true at the deadline?", "suggested": goal["statement"],
              "why": f"{num(demand)} {edition['name']} planned, {num(open_)} still to pack (launch plan, pack-out)."},
             {"id": "unit_loss", "q": "What does one missing copy cost?", "suggested": str(goal["unit_loss_eur"]),
              "unit": "EUR", "why": f"Goal answer for this run ({SCN}); no kb fact states a refund per copy."},
             {"id": "sacrifice", "q": "What may suffer to get there?", "suggested": goal["sacrifice"],
              "why": f"Goal answer for this run ({SCN})."},
             {"id": "budget", "q": "How much can the agent commit alone, per hedge?",
              "suggested": str(goal["budget_eur"]), "unit": "EUR",
              "why": f"Goal answer for this run ({SCN}). The approval policy still decides who signs."}]})
    add({"kind": "clock", "now": start.isoformat()})
    for sid in sorted({c for c in w.history.suppliers}):
        add(supplier_record(w, w.card(sid), masters.get(sid, {}), sid, w.parts_of(sid)))
    add({"kind": "cue", "label": "Goal interview", "wait_for": "goal"})
    at = lambda t: instant(t).isoformat()
    t_plan = start.replace(minute=start.minute + 1).isoformat()
    lane0 = lanes(w, cov, spots, edition)
    add(plan(t_plan, lane0, covered, open_))
    add({"kind": "event", "type": "agent", "at": t_plan, "title": f"Weak spots found: {lane0[0]['name']} is critical",
         "lane": part, "severity": "risk", "impact": {"units": at_risk, "eur": exposure},
         "detail": {"happened": top["reason"],
                    "actions": [{"type": "plan", "title": "Parts ranked by money at risk", "status": "done"}],
                    "reasoning": [{"text": "Open copies", "value": num(open_)},
                                  {"text": "Copies the stock on hand can pack", "value": num(covered)},
                                  {"text": "Copies at risk", "value": num(at_risk)},
                                  {"text": "At stake", "value": f"{num(at_risk)} x {eur(goal['unit_loss_eur'])} = "
                                                                f"{eur(exposure)}"}],
                    "autonomy": {"mode": "autonomous", "text": "Analysis only."}}})
    add({"kind": "cue", "label": "Offer from A"})
    add({"kind": "clock", "now": at(back["ordered_at"])})
    add(supplier_record(w, a_card, a_master, off["supplier_id"], [part]))
    add({"kind": "event", "type": "offer_in", "at": at(off["received_at"]),
         "title": f"{off['label']} offers {num(off['units'])} x {w.part_name(part)}", "lane": part,
         "supplier_id": off["supplier_id"], "severity": "info",
         "detail": {"happened": f"Offer: {num(off['units'])} at {eur(off['unit_price_eur'])}, loading "
                                f"{instant(off['loads_at']):%a %H:%M} ({SCN}).",
                    "autonomy": {"mode": "none", "text": "Incoming offer."}}})
    add({"kind": "event", "type": "agent", "at": at(back["ordered_at"]),
         "title": f"Accept {off['label']} + backup lot from {b_name}", "lane": part, "severity": "info",
         "impact": {"eur": deposit},
         "detail": {"happened": result["rationale"] if isinstance(result["rationale"], str)
                    else " ".join(map(str, result["rationale"])),
                    "reasoning": [{"text": r.get("text"), "value": shown(r)}
                                  for r in result["drilldown"]["inputs"]][:8],
                    "autonomy": {"mode": "autonomous",
                                 "text": f"Deposit {eur(deposit)} is under the goal's {eur(goal['budget_eur'])}."}}})
    add({"kind": "promise", "promise_id": "P-B-LOAD", "supplier_id": b_id, "lane": part,
         "what": f"{num(back['units'])} loaded", "due": at(back["load_due"]), "proof": "CMR scan", "state": "waiting",
         "next_check": at(back["load_due"]), "fallback": "Call the supplier, then escalate to the buyer"})
    add({"kind": "promise", "promise_id": "P-A-LOAD", "supplier_id": off["supplier_id"], "lane": part,
         "what": f"{num(off['units'])} loaded", "due": at(off["loads_at"]), "proof": "CMR scan", "state": "waiting",
         "next_check": at(off["loads_at"]), "fallback": f"Rely on the {b_name} lot"})
    add({"kind": "cue", "label": "Check-in"})
    add({"kind": "clock", "now": at(back["reply_at"])})
    add({"kind": "event", "type": "email_in", "at": at(back["reply_at"]), "title": f"{b_name}: loading by "
         f"{instant(back['load_due']):%H:%M}", "lane": part, "supplier_id": b_id, "severity": "ok",
         "detail": {"happened": f"Reply from {b_name} ({SCN}).", "autonomy": {"mode": "none", "text": ""}}})
    add({"kind": "cue", "label": "Jump to the cancellation"})
    add({"kind": "clock", "now": at(back["loaded_at"])})
    add({"kind": "promise", "promise_id": "P-B-LOAD", "state": "proven", "proven_at": at(back["loaded_at"])})
    broken = w.card(off["history_from"], [{"supplier_id": off["history_from"], "state": "broken",
                                           "promise_id": "P-A-LOAD"}])
    add({"kind": "clock", "now": at(canc["at"])})
    add({"kind": "event", "type": "cancellation", "at": at(canc["at"]),
         "title": f"{off['label']} cancels: {canc['reason']}", "lane": part, "supplier_id": off["supplier_id"],
         "severity": "risk", "impact": {"units": at_risk, "eur": exposure},
         "detail": {"happened": f"{off['label']} never loaded ({SCN}).",
                    "reasoning": [{"text": "At stake", "value": f"{num(at_risk)} copies x "
                                                              f"{eur(goal['unit_loss_eur'])} = {eur(exposure)}"}],
                    "autonomy": {"mode": "needs_decision", "text": f"Keeping the {b_name} lot commits "
                                                                  f"{eur(remaining)}."}}})
    add({"kind": "promise", "promise_id": "P-A-LOAD", "state": "broken", "broken_at": at(canc["at"])})
    add(supplier_record(w, broken, a_master, off["supplier_id"], [part], at=at(canc["at"]),
                        cause="cancelled its order"))
    add({"kind": "cue", "label": "Decision"})
    add({"kind": "clock", "now": at(scn["decision_at"])})
    add({"kind": "decision", "decision_id": "D-BACKUP-CONFIRM", "status": "open", "at": at(scn["decision_at"]),
         "lane": part, "supplier_id": b_id, "title": f"Confirm {b_name} in full",
         "recommendation": f"Keep all {num(back['units'])} from the {b_name} truck arriving "
                           f"{instant(back['arrives_at']):%H:%M}.",
         "impact": {"units": at_risk, "eur": exposure, "text": f"protects {num(at_risk)} {edition['name']} copies"},
         "cost": {"eur": remaining, "text": f"{eur(remaining)} remaining ({num(back['units'])} x {eur(unit_price)} "
                                            f"minus the {eur(deposit)} deposit)"},
         "signer": {"role": signer.get("role"), "name": signer.get("name"), "rule": rule,
                    "status": signer.get("status"), "fact_ids": signer.get("fact_ids", [])},
         "on_accept": [f"Confirmation to {b_name}", f"Cancellation acknowledged to {off['label']}"],
         "options": []})
    add({"kind": "cue", "label": "Accepted", "wait_for": "accept:D-BACKUP-CONFIRM"})
    end = instant(scn["decision_at"]).replace(minute=instant(scn["decision_at"]).minute + 1).isoformat()
    add({"kind": "clock", "now": end})
    add({"kind": "promise", "promise_id": "P-B-ARRIVE", "supplier_id": b_id, "lane": part,
         "what": f"{num(back['units'])} received at {site.get('site_id')}", "due": at(back["arrives_at"]),
         "proof": "Goods receipt", "state": "waiting", "next_check": at(back["arrives_at"])})
    after = lanes(w, cov, spots, edition, {part: int(back["units"] / per_copy)})
    covers = open_ - max([0] + [l["gap"] for l in after])   # the scarcest part limits the edition
    add(plan(end, after, covers, open_))
    short = [l for l in after if l["gap"] > 0]
    reasoning = [{"text": "Bet", "value": eur(deposit)}, {"text": "Protected", "value": eur(exposure)}]
    if short:
        nxt = next((s for s in spots if s["part_id"] == short[0]["lane_id"]), {})
        reasoning.append({"text": f"Next weak spot: {short[0]['name']}", "value": nxt.get("reason", "")})
    add({"kind": "event", "type": "goal", "at": end,
         "title": (f"Goal met: {num(covers)} of {num(open_)} {edition['name']} covered" if not short else
                   f"{w.part_name(part)} secured. {num(covers)} of {num(open_)} {edition['name']} covered; "
                   f"{short[0]['name']} still {num(short[0]['gap'])} short"),
         "severity": "ok" if not short else "risk",
         "detail": {"reasoning": reasoning, "autonomy": {"mode": "none", "text": ""}}})
    add({"kind": "cue", "label": "End"})
    return recs
