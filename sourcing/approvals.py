"""Who signs: approval routing from the policy and org chart facts.

    result = route(rows, action="purchase_order", on=clock.now(conn), amount_eur=total, amount_source=source,
                   category=category)

`rows` are rows of `kb.current_facts`: the policy, the org charts with their `org_seat` facts and, to link a
delivery site to its seat, the site facts. No database, no LLM, and it approves nothing: it names who has to.
A name comes only from a seat with status held, a holder and a date window that covers the day. A vacant, absent
or unclear seat, or none at all, gives `needs_human` with the seat and, where the policy has one, the role that
approves exceptions. That role is never turned into a name here.
`result["signer"]` fits the `decision` contract. It carries a name only when `status` is `resolved`; with `held`
or `needs_human` the reasons say what a person has to settle first.
`on` decides which chart and which holder are valid, so pass the time in the team's own time zone.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from sourcing.approval_chart import HELD, Chart, cited_lines, read_charts, seat_scope, title_scope
from sourcing.approval_docs import CHART_KIND, POLICY_KIND, Policy, Rule, Section, documents, read_policy, words
from sourcing.approval_sites import Site, read_sites, seat_scopes

ACTIONS = ("purchase_order", "air_freight", "conditional_release", "release_extension")
UNRESOLVED = "Unresolved"
CURRENCY = "EUR"                   # the unit of every amount in plan contracts (`*_eur`)


def _money(value, name: str) -> Decimal | None:
    if value is None:
        return None
    try:
        amount = Decimal(str(value)) if not isinstance(value, bool) else None
    except InvalidOperation:
        amount = None
    if amount is None or not amount.is_finite() or amount < 0:
        raise ValueError(f"{name} must be a finite amount >= 0, got {value!r}")
    return amount


def _plain(value: Decimal | None):
    if value is None:
        return None
    return int(value) if value == value.to_integral_value() else float(value)


def _version(value: str | None) -> tuple:
    return tuple(int(x) for x in value.split(".")) if value else ()


def _policy(rows: list[dict], today: date) -> tuple[Policy | None, list[str]]:
    found = [p for p in map(read_policy, documents(rows, POLICY_KIND)) if p]
    usable = [p for p in found if p.effective is None or p.effective <= today]
    undated = [f"{p.path}: no readable effective date" for p in found if p.effective is None]
    if not usable:
        return None, ["no approval policy in force was found in the facts"]
    rank = lambda p: (p.effective or date.min, _version(p.version))
    best = max(usable, key=rank)

    def terms(p: Policy):
        rules = (p.air, p.release, p.release and p.release.extension, p.site, p.exceptions)
        return (p.currency, [(t.roles, t.op, t.amount) for t in p.tiers],
                [(r.roles, r.above, r.limit, r.currency, r.days, r.once) if r else None for r in rules])

    problems = undated + best.problems     # a policy without a date could be the newer one
    others = sorted({p.path for p in usable if not p.same_policy(best)})
    if others:
        problems.append(f"different policies state approval limits: {best.path} and {', '.join(others)}")
    if any(rank(p) == rank(best) and terms(p) != terms(best) for p in usable):
        problems.append(f"two readings of {best.name} with the same version disagree")
    return best, problems


def _chart(rows: list[dict], today: date) -> tuple[Chart | None, list[Chart], list[str]]:
    found, undated = read_charts(rows, documents(rows, CHART_KIND))
    undated += [p for c in found if c.dated is None for p in c.problems]
    usable = [c for c in found if c.dated and c.dated <= today]
    if not usable:
        return None, found, undated + ["no org chart in force was found in the facts"]
    rank = lambda c: (c.dated, _version(c.version))
    best = max(usable, key=rank)
    seats = lambda c: sorted((s.title, s.scope, s.name or "", s.state, str(s.start), str(s.end)) for s in c.seats)
    problems = undated + best.problems     # a chart without a date could be the newer one
    if any(rank(c) == rank(best) and seats(c) != seats(best) for c in usable):
        problems.append("two org charts with the same version and date disagree")
    return best, found, problems


def _holder(chart: Chart, role: str, area: str | None, today: date, sites: list[Site] | None = None) -> dict:
    """The seat of a role on that day: state is held, vacant, absent, unclear, not_started, ended or not_in_chart.

    With `sites` the area is a delivery site, otherwise a category, which matches exactly. Every seat that matches
    has to be held on that day by the same person, otherwise nobody signs."""
    matches = [(s, scope) for s in chart.seats if (scope := seat_scope(s, role)) is not None]
    site_ids: list[str] = []
    if area and sites is not None:
        fitting, site_ids = seat_scopes({scope for _, scope in matches}, area, sites)
        matches = [m for m in matches if m[1] in fitting]
    elif area:
        matches = [m for m in matches if set(words(m[1])) == set(words(area))] or [m for m in matches if not m[1]]
    elif any(scope for _, scope in matches):
        return {"state": "unclear", "why": f"{role} is held per category or site and none was given"}
    if not matches:
        where = f" for {area}" if area else ""
        if sites is not None:
            where += " that can be linked to it by the site's id or name"
        return {"state": "not_in_chart", "why": f"the org chart has no seat for {role}{where}", "fact_ids": site_ids}

    seats = [s for s, _ in matches]
    out = {"title": seats[0].title, "fact_ids": site_ids + [i for s in seats for i in s.ids],
           "seat_fact_ids": [i for s in seats for i in s.ids],
           "line_keys": list(dict.fromkeys(s.line_key for s in seats if s.line_key))}
    names = {s.name for s in seats}
    states = {s.state_on(today) for s in seats}
    if len(states) > 1:
        return {**out, "state": "unclear",
                "why": f"the seats for {seats[0].title} disagree: " + ", ".join(sorted(states))}
    if states == {HELD} and len(names) > 1:
        return {**out, "state": "unclear", "why": f"several people hold {role}: " + ", ".join(sorted(names))}
    state = states.pop()
    if state == HELD:
        return {**out, "state": HELD, "name": seats[0].name}
    why = f"the seat {seats[0].title} is {state.replace('_', ' ')} on {today.isoformat()}"
    if state == "not_started":
        why += f" (starts {min(s.start for s in seats).isoformat()})"
    return {**out, "state": state, "why": why}


class _Routing:
    def __init__(self, rows: list[dict], action: str, today: date):
        self.action, self.today = action, today
        self.rows, self.sites = rows, read_sites(rows)
        self.reasons: list[str] = []
        self.sources: list = []
        self.signers: list[dict] = []
        self.also: list[dict] = []
        self.extra: dict = {}
        self.rule: str | None = None
        self.held = self.blocked = False
        self.chart, self.charts = None, []
        self.policy, problems = _policy(rows, today)
        if self.policy:
            self.cite(self.policy.ids)
            self.chart, self.charts, more = _chart(rows, today)
            problems += more
            if self.chart:
                self.cite(self.chart.ids)
        for problem in problems:
            self.stop(problem)

    def stop(self, why: str) -> None:
        self.blocked = True
        self.reasons.append(why)

    def cite(self, fact_ids) -> None:
        for fact_id in fact_ids:
            if {"fact_id": fact_id} not in self.sources:
                self.sources.append({"fact_id": fact_id})

    def given(self, value: Decimal, source, name: str) -> dict:
        if not source or not isinstance(source, (str, dict)):
            raise ValueError(f"{name} needs its source (fact id, goal line or labelled scenario input)")
        self.sources.append(source)
        return {"value": _plain(value), "currency": CURRENCY, "source": source}

    def label(self, section: Section, detail: str = "") -> str:
        version = f" v{self.policy.version}" if self.policy.version else ""
        return f"{self.policy.name}{version} section {section.number} ({section.title}){detail}"

    def use(self, section: Section, detail: str = "", ids=None) -> str:
        self.cite(section.ids if ids is None else [section.ids[0], *ids])
        return self.label(section, detail)

    def rank(self, role: str) -> int | None:
        for i, tier in enumerate(self.policy.tiers):
            for other in tier.roles:
                if words(other) == words(role) or title_scope(other, role) == "" or title_scope(role, other) == "":
                    return i
        return None

    def sign(self, role: str, rule: str, area: str | None, sites: list[Site] | None = None) -> dict:
        signer = {"role": role, "required_role": role, "rule": rule}
        if not self.chart:
            return {**signer, "state": "unclear"}
        found = _holder(self.chart, role, area, self.today, sites)
        self.cite(found.get("fact_ids", []))
        signer.update(role=found.get("title", role), state=found["state"])
        if "seat_fact_ids" in found:
            signer.update(seat_fact_ids=found["seat_fact_ids"], line_keys=found["line_keys"])
            self.cite(cited_lines(self.rows, self.chart, found["line_keys"]))
        if found["state"] == HELD:
            return {**signer, "name": found["name"]}
        self.stop(found["why"])
        # an unreadable seat, or a site nobody recognises, is a lookup failure and no exception to the policy
        if found["state"] == "unclear" or (sites is not None and found["state"] == "not_in_chart"):
            return signer
        return self.exception(signer, role)

    def exception(self, signer: dict, role: str) -> dict:
        """Escalation per the exceptions section: the role to put it to, for a person to act on. Never a name."""
        rule = self.policy.exceptions
        if not rule or len(rule.roles) != 1:
            self.reasons.append("the policy names no single approver for exceptions")
            return signer
        approver, why = rule.roles[0], self.use(rule.section)
        needed, limit = self.rank(role), self.rank(approver)
        if needed is not None and (limit is None or needed > limit):
            self.reasons.append(f"{approver} approves exceptions but has a lower limit than {role}: a person has to "
                                "decide")
            return signer
        self.reasons.append(f"exceptions are approved by the {approver} per {why}: a person has to decide")
        return {**signer, "exception_approver": {"role": approver, "rule": why,
                                                 "fact_ids": rule.section.ids}}

    def value(self, amount: Decimal | None, source, category: str | None) -> None:
        policy = self.policy
        if amount is None:
            return self.stop("the order value is unknown, so no limit can be applied")
        self.extra["amount"] = self.given(amount, source, "amount_eur")
        if policy.currency != CURRENCY:
            return self.stop(f"the policy states its limits in {policy.currency}, the amount is in {CURRENCY}")
        tier = next((t for t in policy.tiers if t.covers(amount)), None)
        if not tier:
            return self.stop(f"no limit in {policy.name} covers this amount")
        self.rule = self.use(policy.value, f": {' and '.join(tier.roles)} {tier.op} {tier.amount:,} {CURRENCY}",
                             tier.ids)
        self.extra["limit"] = {"op": tier.op, "value": _plain(tier.amount), "currency": CURRENCY,
                               "fact_ids": tier.ids}
        self.signers = [self.sign(role, self.rule, category) for role in tier.roles]

    def site(self, delivery_site: str) -> None:
        rule = self.policy.site
        if rule:
            why = self.use(rule.section)
            if not rule.roles:
                return self.stop(f"cannot read who releases the delivery in {why}")
            self.also = [self.sign(role, why, delivery_site, self.sites) for role in rule.roles]

    def air(self, amount: Decimal | None, source, category: str | None, used: Decimal | None, used_source) -> None:
        rule = self.policy.air
        self.extra["budget"] = budget = {"used": "unknown", "limit": "unknown", "category": category or "unknown"}
        if used is not None:
            budget["used"] = self.given(used, used_source, "budget_used_eur")
        if amount is not None:
            self.extra["amount"] = self.given(amount, source, "amount_eur")
        if not rule or rule.limit is None or not rule.roles or not rule.above:
            return self.stop("no readable air freight rule in the policy")
        self.rule = self.use(rule.section)
        if rule.currency != CURRENCY:
            return self.stop(f"the air freight limit is in {rule.currency}, the amount is in {CURRENCY}")
        budget["limit"] = {"value": _plain(rule.limit), "currency": CURRENCY, "fact_ids": rule.section.ids}
        if amount is None:
            return self.stop("the air freight cost is unknown, so no limit can be applied")
        if amount > rule.limit or (used is not None and used + amount > rule.limit):
            roles = rule.above
            self.reasons.append("this booking takes the month and category above the limit")
        elif used is not None:
            roles = rule.roles
            if not category:
                return self.stop("the category is unknown, and the limit is per category")
        else:
            roles = rule.above
            budget["lower_signer_if_used_at_most"] = {"role": " and ".join(rule.roles),
                                                      "value": _plain(rule.limit - amount), "currency": CURRENCY,
                                                      "fact_ids": rule.section.ids}
            self.reasons.append("the category budget used this month is unknown, so the approval above the limit "
                                "applies")
        self.signers = [self.sign(role, self.rule, category) for role in roles]

    def release(self, category: str | None, released_on: date | None, source, extended: int | None) -> None:
        base: Rule | None = self.policy.release
        extending = self.action == "release_extension"
        rule = base.extension if base and extending else base
        if not rule or not rule.roles:
            return self.stop(f"no readable rule for {self.action} in the policy")
        self.rule = self.use(rule.section)
        validity = {"max_days": rule.days if rule.days is not None else "unknown", "fact_ids": rule.section.ids}
        if released_on:
            if not source:
                raise ValueError("released_on needs its source")
            self.sources.append(source)
        if not extending and rule.days is not None:
            validity["valid_until"] = ((released_on or self.today) + timedelta(days=rule.days)).isoformat()
        self.extra["validity"] = validity
        self.signers = [self.sign(role, self.rule, category) for role in rule.roles]
        if extending and rule.once:
            if extended is None:
                self.stop("it is unknown whether the release was extended before, and it may be extended once")
            elif extended >= 1:
                self.stop("the release was already extended, and it may be extended once")

    def expired(self, valid_until: date, source) -> None:
        if not source:
            raise ValueError("release_valid_until needs its source")
        self.sources.append(source)
        if valid_until < self.today:
            self.held = True
            self.reasons.append(f"the supplier's conditional release expired on {valid_until.isoformat()}: the "
                                "order waits for approval")
            if self.policy.release:
                self.reasons.append(f"held per {self.use(self.policy.release.section)}")

    def result(self) -> dict:
        everyone = self.signers + self.also
        people = [" ".join(words(s["name"])) for s in self.signers if "name" in s]
        if len(set(people)) < len(people):
            self.stop("the rule needs two people and one person would sign for both: a person has to decide")
        if self.blocked or not self.signers or any(s["state"] != HELD for s in everyone):
            status = "needs_human"
        elif self.held:
            status = "held"
        else:
            status = "resolved"
        named = status == "resolved"
        signer = {"role": " and ".join(dict.fromkeys(s["role"] for s in self.signers)) or UNRESOLVED}
        if named:
            signer["name"] = " and ".join(dict.fromkeys(s["name"] for s in self.signers))
        rules = list(dict.fromkeys(s["rule"] for s in self.signers)) or ([self.rule] if self.rule else [])
        if rules:
            signer["rule"] = "; ".join(rules)
        return {"action": self.action, "on": self.today.isoformat(), "status": status, "signer": signer,
                "signers": self.signers, "also_required": self.also,
                "policy": self.policy and {"name": self.policy.name, "version": self.policy.version,
                                           "path": self.policy.path, "fact_ids": self.policy.ids},
                "org_chart": self.chart_used(), "reasons": self.reasons, "sources": self.sources, **self.extra}

    def chart_used(self) -> dict | None:
        chart = self.chart
        if not chart:
            return None
        used = f"org chart v{chart.version} of {chart.dated.isoformat()}"
        older = sorted({(c.dated, c.version, c.path) for c in self.charts
                        if c.dated and (c.dated, _version(c.version)) < (chart.dated, _version(chart.version))})
        return {"label": used, "version": chart.version, "dated": chart.dated.isoformat(), "path": chart.path,
                "fact_ids": chart.ids,
                "replaces": [{"version": v, "dated": d.isoformat(), "path": p} for d, v, p in older]}


def route(rows: list[dict], *, action: str, on: date | datetime, amount_eur=None, amount_source=None,
          category: str | None = None, delivery_site: str | None = None, budget_used_eur=None,
          budget_used_source=None, released_on: date | None = None, extensions_used: int | None = None,
          release_valid_until: date | None = None, release_source=None) -> dict:
    """Who signs this action on that day, with the rule, the chart used and the sources."""
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {', '.join(ACTIONS)}, got {action!r}")
    if isinstance(on, datetime) and on.tzinfo is None:
        raise ValueError("on needs a UTC offset")
    amount, used = _money(amount_eur, "amount_eur"), _money(budget_used_eur, "budget_used_eur")
    routing = _Routing(list(rows), action, on.date() if isinstance(on, datetime) else on)
    if routing.policy:
        if action == "purchase_order":
            routing.value(amount, amount_source, category)
            if delivery_site:
                routing.site(delivery_site)
            if release_valid_until:
                routing.expired(release_valid_until, release_source)
        elif action == "air_freight":
            routing.air(amount, amount_source, category, used, budget_used_source)
        else:
            routing.release(category, released_on, release_source, extensions_used)
    return routing.result()
