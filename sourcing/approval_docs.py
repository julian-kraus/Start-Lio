"""Read the approval policy out of kb facts (rows of `kb.current_facts`).

Pure and deterministic. The code knows how such documents are written (numbered sections, a limit table), not
what they say: roles, names, amounts, dates and document ids all come from the facts.
Whatever cannot be read is reported as a problem, so that routing asks a human instead of guessing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

POLICY_KIND, CHART_KIND = "policy_document", "org_chart"
# Files a supplier sent are claims, never our own rules, whatever kind they were classified as.
SUPPLIER_SENT = ("mail/", "suppliers/")
MIN_CONFIDENCE = 0.6

SPACE = " \u00a0\u202f"
NUMBER = rf"\d{{1,3}}(?:(?P<sep>[,.'{SPACE}])\d{{3}})(?:(?P=sep)\d{{3}})*(?:[.,]\d{{1,2}})?|\d+(?:[.,]\d{{1,2}})?"
OPS = {"<": "<", "under": "<", "below": "<", "<=": "<=", "\u2264": "<=", "up to": "<=",
       ">": ">", "over": ">", "above": ">", ">=": ">=", "\u2265": ">=", "from": ">="}
LIMIT_START = r"(?:<=|>=|[<>\u2264\u2265]|(?i:up to|under|below|over|above|from)\s+(?=(?:[A-Z]{3}\s+)?\d))"
LIMIT_CELL = re.compile(rf"(?:(?P<role>[^<>\u2264\u2265:|]+?)[\s:|]+)?(?P<op>{LIMIT_START})\s*(?P<rest>.+)")
HEADING = re.compile(r"(\d{1,2})\.\s+([A-Z].{0,100}?[^.,;:])")
WORD = r"[A-Z][\w&'-]*"
ROLE = rf"{WORD}(?:\s+(?:of\s+|&\s+|for\s+)?{WORD})*"
BY_ROLE = re.compile(
    rf"(?:(?i:approved|approval|released|release|signed|extended)\b[^.]*?\bby|together with|\band)\s+the\s+"
    rf"(?:responsible\s+)?({ROLE})")
VERSION_OR_DATE = re.compile(r"\bv(?:ersion\s*)?\d+(?:\.\d+)*\b|\d{4}-\d\d-\d\d", re.I)


@dataclass(frozen=True)
class Line:
    text: str
    fact_id: str
    confidence: float


@dataclass
class Document:
    path: str
    kind: str
    scope: str | None
    lines: list[Line]
    problems: list[str] = field(default_factory=list)


@dataclass
class Section:
    number: int
    title: str
    lines: list[Line]

    @property
    def text(self) -> str:
        return " ".join(x.text for x in self.lines[1:])

    @property
    def ids(self) -> list[str]:
        return [x.fact_id for x in self.lines]


@dataclass
class Tier:
    roles: list[str]
    op: str
    amount: Decimal
    ids: list[str]

    def covers(self, amount: Decimal) -> bool:
        return {"<": amount < self.amount, "<=": amount <= self.amount,
                ">": amount > self.amount, ">=": amount >= self.amount}[self.op]


@dataclass
class Rule:
    """A prose rule: who signs, and the numbers it states."""
    section: Section
    roles: list[str]
    above: list[str] = field(default_factory=list)
    limit: Decimal | None = None
    currency: str | None = None
    days: int | None = None
    once: bool = False
    extension: "Rule | None" = None


@dataclass
class Policy:
    path: str
    name: str
    version: str | None
    effective: date | None
    problems: list[str]
    ids: list[str]
    value: Section | None = None
    currency: str | None = None
    tiers: list[Tier] = field(default_factory=list)
    air: Rule | None = None
    release: Rule | None = None
    site: Rule | None = None
    exceptions: Rule | None = None
    scope: str | None = None
    title: str = ""

    def same_policy(self, other: "Policy") -> bool:
        """Versions or readings of one policy, as opposed to another policy that also states limits."""
        if self.path == other.path:
            return True
        if self.scope and other.scope:
            return words(self.scope) == words(other.scope)
        if self.scope or other.scope:
            scope, title = (self.scope, other.title) if self.scope else (other.scope, self.title)
            return set(words(scope)) <= set(words(title))
        return words(VERSION_OR_DATE.sub("", self.title)) == words(VERSION_OR_DATE.sub("", other.title))

    def role_terms(self) -> list[str]:
        roles = [r for t in self.tiers for r in t.roles]
        for rule in (self.air, self.release, self.release and self.release.extension, self.site, self.exceptions):
            if rule:
                roles += rule.roles + rule.above
        return list(dict.fromkeys(roles))


def number(text: str) -> Decimal | None:
    """An amount as printed ("10 000", "10,000.50", "10.000"). None when it is anything else."""
    text = text.strip()
    match = re.fullmatch(NUMBER, text)
    if not match:
        return None
    if match["sep"]:
        groups = re.fullmatch(r"(.+?)(?:[.,](\d{1,2}))?", text)
        return Decimal(re.sub(r"\D", "", groups[1]) + (f".{groups[2]}" if groups[2] else ""))
    return Decimal(text.replace(",", "."))


def iso_date(text: str | None) -> date | None:
    try:
        return date.fromisoformat(text) if text else None
    except ValueError:
        return None


def ours(row: dict) -> bool:
    return not (row.get("path") or "").startswith(SUPPLIER_SENT) and not (row.get("locator") or {}).get("mail")


def documents(rows: list[dict], kind: str) -> list[Document]:
    """The text lines of our own documents of one kind, in reading order, one per file and extractor."""
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        path = row.get("path") or ""
        if row.get("doc_kind") != kind or not ours(row):
            continue
        if row.get("attribute") not in ("directive", "text") or not isinstance(row.get("value"), str):
            continue
        groups.setdefault((path, row.get("sha256") or "", row.get("extractor") or ""), []).append(row)
    out = []
    for (path, _, _), facts in sorted(groups.items()):
        doc = Document(path, kind, next((str(f["subject"]["scope"]) for f in facts
                                         if (f.get("subject") or {}).get("scope")), None), [])
        where = [((f.get("locator") or {}).get("page", 1), (f.get("locator") or {}).get("line")) for f in facts]
        if len(facts) > 1:
            if not all(isinstance(p, int) and isinstance(n, int) for p, n in where) or len(set(where)) < len(where):
                doc.problems.append(f"{path}: the reading order of the lines is unknown")
            else:
                facts = [f for _, f in sorted(zip(where, facts), key=lambda pair: pair[0])]
        for f in facts:
            conf = 1.0 if f.get("confidence") is None else float(f["confidence"])
            doc.lines += [Line(t.strip(), str(f["fact_id"]), conf) for t in f["value"].splitlines() if t.strip()]
        if any(x.confidence < MIN_CONFIDENCE for x in doc.lines):
            doc.problems.append(f"{path}: lines were read with low confidence")
        out.append(doc)
    return out


def sections(lines: list[Line]) -> list[Section]:
    out: list[Section] = []
    for line in lines:
        head = HEADING.fullmatch(line.text)
        if head and int(head[1]) > (out[-1].number if out else 0):
            out.append(Section(int(head[1]), head[2], [line]))
        elif out:
            out[-1].lines.append(line)
    return out


def joint_roles(text: str) -> list[str]:
    cleaned = re.sub(r"\s+(jointly|together)$", "", text.strip(), flags=re.I)
    return re.split(r"\s+and\s+", cleaned) if cleaned != text.strip() else [cleaned]


def _tiers(section: Section, policy: Policy) -> None:
    in_heading = re.search(r"\bin ([A-Z]{3})\b", section.lines[0].text)
    policy.currency = in_heading[1] if in_heading else None
    body = section.lines[1:]
    for i, line in enumerate(body):
        match = LIMIT_CELL.fullmatch(line.text)
        if not match:
            if re.search(r"\d[,.']\d{3}", line.text) or re.match(r"(?:[A-Z]{3}\s*)?\d", line.text):
                policy.problems.append(f"section {section.number}: cannot read the amount in {line.text!r}")
            continue
        rest = re.fullmatch(r"(?:([A-Z]{3})\s*)?(.+?)(?:\s*([A-Z]{3}))?", match["rest"].strip())
        amount = number(rest[2])
        role = match["role"] or (body[i - 1].text if i else "")
        if amount is None or not role or LIMIT_CELL.fullmatch(role):
            policy.problems.append(f"section {section.number}: cannot read the limit {line.text!r}")
            continue
        stated = rest[1] or rest[3]
        policy.currency = policy.currency or stated
        if stated and stated != policy.currency:
            policy.problems.append(f"section {section.number}: limits in more than one currency")
        ids = [body[i - 1].fact_id, line.fact_id] if not match["role"] else [line.fact_id]
        policy.tiers.append(Tier(joint_roles(role), OPS[match["op"].strip().lower()], amount, ids))
    uppers = [t.amount for t in policy.tiers if t.op in ("<", "<=")]
    floors = [i for i, t in enumerate(policy.tiers) if t.op in (">", ">=")]
    if uppers != sorted(set(uppers)) or floors not in ([], [len(policy.tiers) - 1]):
        policy.problems.append(f"section {section.number}: the limits are not in ascending order")
    if not policy.tiers or not policy.currency:
        policy.problems.append(f"section {section.number}: no readable limit table with a currency")


def _roles(text: str) -> list[str]:
    return list(dict.fromkeys(BY_ROLE.findall(text)))


def _air(section: Section) -> Rule:
    within, *above = re.split(r"(?<=[.;])\s+(?=Above\b|Over\b|Beyond\b)", section.text, maxsplit=1)
    rule = Rule(section, _roles(within), _roles(above[0]) if above else [])
    limit = re.search(rf"\bup to\s+([A-Z]{{3}})\s+({NUMBER})(?![\d.,'])(?!\s*(?:\d|k\b|m\b|million|thousand))",
                      within)
    if limit:
        rule.currency, rule.limit = limit[1], number(limit[2])
    return rule


def _release(section: Section) -> Rule:
    rule = Rule(section, [])
    for sentence in re.split(r"(?<=\.)\s+", section.text):
        if re.search(r"\bextended\b", sentence, re.I):
            days = re.search(r"\bat most (\d+) days\b", sentence)
            rule.extension = Rule(section, _roles(sentence), days=int(days[1]) if days else None,
                                  once=bool(re.search(r"\bonce\b", sentence)))
            continue
        days = re.search(r"\blimited to (\d+) days\b", sentence)
        if days:
            rule.days = int(days[1])
        elif not rule.roles:
            rule.roles = _roles(sentence)
    return rule


def read_policy(doc: Document) -> Policy | None:
    """None when the document has no table of approval limits, so it is some other policy."""
    parts = sections(doc.lines)
    value = next((s for s in parts if re.search(r"\blimits?\b", s.title, re.I)), None)
    if value is None:
        return None
    text = " ".join(x.text for x in doc.lines)
    version = re.search(r"\b(?:Version\s+|v)(\d+(?:\.\d+)*)\b", text)
    effective = re.search(r"\bEffective\s+(\d{4}-\d\d-\d\d)\b", text, re.I)
    policy = Policy(doc.path, doc.scope or doc.lines[0].text, version[1] if version else None,
                    iso_date(effective[1]) if effective else None, list(doc.problems),
                    [x.fact_id for x in doc.lines if (version and version[0] in x.text)
                     or (effective and effective[0] in x.text)], value,
                    scope=doc.scope, title=doc.lines[0].text)
    _tiers(value, policy)
    for section in parts:
        if section is value:
            continue
        if re.search(r"\bair[ -]?freight\b", section.title, re.I):
            policy.air = _air(section)
        elif re.search(r"\bexceptions?\b", section.title, re.I):
            policy.exceptions = Rule(section, _roles(section.text))
        elif re.search(r"\bconditional release", section.text, re.I):
            policy.release = _release(section)
        elif re.search(r"\bin addition to the value approval\b", section.text):
            who = re.search(r"\breleased by the ([A-Za-z ]+?)[.,]", section.text)
            policy.site = Rule(section, [who[1]] if who else [])
    return policy


def words(text: str) -> list[str]:
    return re.findall(r"[^\W_]+(?:'[^\W_]+)?", text.replace("\u2019", "'").replace("&", " and ").casefold())
