"""What a goal answer does and does not say around a quote. Pure functions, no model.

The model can crop a quote, mislabel it or leave a statement out. These checks look at the words around the quote
and at what no item accounts for. Where code cannot tell what was meant, the point stays open; nothing is guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping

from . import interview_text as text

REFUSAL = re.compile(
    r"\bn(?:o|ot)\b(?! (?:care|mind|matter|need|important|critical|urgent|problem)\b)"
    r"|\b\w+n't\b(?! (?:care|mind|matter|need)\b)|\b(?:dont|cant|wont|shouldnt|mustnt)\b"
    r"|\b(?:never|nobody|none|nothing|neither|nor|without|cannot|refus\w*|protect\w*|unacceptable|untouch\w*"
    r"|off.limits|sacred|intact|avoid\w*|forbid\w*|forbidden|prevent\w*|keep|rule out|nicht|kein\w*|nie|niemals"
    r"|hate\w*|dislike\w*|detest\w*|loath\w*|dread\w*|doubt\w*|afraid|fear\w*|worr(?:y|ied|ies)|disaster\w*"
    r"|hardly|barely|scarcely|last thing|unless|only if)\b",
    re.IGNORECASE)
# "suffers" alone describes a loss; only a word of permission accepts it
ACCEPTS = re.compile(
    r"\b(?:can|may|could|might|ok|okay|fine|acceptable|accept\w*|sacrific\w*|give up|given up|expendable|let"
    r"|waits?|slip|secondary|not important|(?:don't|do not) (?:care|mind)|(?:doesn't|does not) matter)\b",
    re.IGNORECASE)
GIVEN_UP = re.compile(r"\b(?:(?:can|may|could|might) (?:wait|suffer|slip|be late)|(?:doesn't|does not) matter)\b",
                      re.IGNORECASE)
TURN = re.compile(r"\b(?:but|however|though|although)\b", re.IGNORECASE)
EVERY = re.compile(r"\b(?:(?:every|all|each|both) editions?|both)\b", re.IGNORECASE)
SPEND = re.compile(r"\b(?:spend\w*|budget|ceiling|cap|at most|no more than)\b", re.IGNORECASE)
PER_COPY = re.compile(r"\b(?:worth|missing|(?:a|per|each) copy|apiece)\b", re.IGNORECASE)
MONEY_TALK = re.compile(r"€|\b(?:eur|euros?|spend\w*|spent|pay|budget|ceiling|cap|costs?|worth|value|price|at most"
                        r"|max\w*|tops|up to|limit|lose|loss|no more than)\b", re.IGNORECASE)
TEAM = re.compile(r"\b(?:team|offline|online|asleep|sleep\w*|reach\w*|available|unavailable|ask|asked|bed|away)\b",
                  re.IGNORECASE)
WHEN = re.compile(r"\b(?:morning|evening|night|noon|midnight|always|anytime|any time|o'clock)\b", re.IGNORECASE)
EURO_WORD = re.compile(r"€|\b(?:eur|euros?)\b", re.IGNORECASE)
NO_MONEY = re.compile(r"\b(?:spend(?:ing)? (?:nothing|anything|no money|zero)|no (?:budget|money)|not (?:spend|pay)"
                      r"|don't (?:spend|pay)(?! (?:more|too))|(?:worth|costs?) nothing|not a (?:cent|penny))\b",
                      re.IGNORECASE)
NUMBER_WORD = re.compile(
    r"\b(?:zero|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen"
    r"|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|thousand|million|noon"
    r"|midnight|one(?= (?:euro|eur|o'clock))|(?<=at )one|(?<=until )one"
    r"|zwei|drei|vier|fünf|sechs|sieben|acht|neun|zehn|zwanzig|fünfzig)\b|" + text.SCALES, re.IGNORECASE)
COUNTED = (r"(?:copies|units|pieces|books|pallets|sets|editions|suppliers|offers|people|days|hours|percent"
           r"|questions)\b")
COMMON_WORDS = {"with", "that", "this", "time", "copy", "copie", "have", "will", "than", "more", "from", "until"}


@dataclass(frozen=True)
class Place:
    """Where an answer says something."""
    question: str
    answer: str
    start: int
    end: int

    @property
    def span(self) -> tuple[int, int]:
        return self.start, self.end

    @property
    def said(self) -> str:
        return self.answer[self.start:self.end]

    def around(self, limit: re.Pattern = text.PART) -> str:
        first, last = text.around(self.answer, self.start, self.end, limit)
        return self.answer[first:last]

    def covers(self, question: str, start: int, end: int) -> bool:
        return self.question == question and self.start <= start and end <= self.end


def named(said: str, editions: list[str]) -> list[str]:
    """The editions the words name. A name that is part of a longer name said here does not count."""
    hits = [e for e in editions if text.names_edition(said, e, editions)]
    have = text.words(said)
    return [e for e in hits if not any(text.words(e) < text.words(o) <= have for o in hits)]


def priority_edition(place: Place, given: str | None, editions: list[str]) -> None:
    clause = place.around()
    hit = named(clause, editions)
    if hit and GIVEN_UP.search(place.said):
        raise text.UnreadableError(f"quote the priority {place.said!r} without what may suffer")
    if given == "all":
        if hit and not EVERY.search(clause):
            raise text.UnreadableError(f"{clause.strip()!r} names {hit}, which is not every edition")
    elif given:
        if given not in hit:
            raise text.UnreadableError(f"the answer does not name {given!r}")
    elif hit:
        raise text.UnreadableError(f"{clause.strip()!r} names {hit}: give the edition")


def value_edition(place: Place, given: str | None, editions: list[str], protected: list[str]) -> None:
    """The edition of a value is the one named where its amount is stated."""
    clause, sentence = place.around(), place.around(text.SENTENCE)
    hit = named(clause, editions)
    if not hit and len(list(text.MONEY.finditer(sentence))) == 1:
        hit = named(sentence, editions)
    several = len(hit) > 1
    if several and len(list(text.MONEY.finditer(clause))) > 1:
        raise text.UnreadableError(f"cannot tell which edition each amount in {clause.strip()!r} is for")
    if given == "all":
        if hit and set(hit) != set(editions) and not EVERY.search(clause):
            raise text.UnreadableError(f"{clause.strip()!r} names {hit}, which is not every edition")
    elif given:
        if several:
            raise text.UnreadableError(f"cannot tell which of {hit} the amount is for")
        if hit != [given] and (hit or named(sentence, editions) or protected != [given]):
            raise text.UnreadableError(f"the answer does not name {given!r} where it states the amount")
    elif hit and set(hit) != set(protected):
        raise text.UnreadableError(f"{clause.strip()!r} names {hit}: give the edition")


def figure_field(place: Place, name: str) -> None:
    """A value is not read from words about spending, nor a ceiling from words about the worth of a copy."""
    clause = place.around()
    mine, other = (PER_COPY, SPEND) if name == "value" else (SPEND, PER_COPY)
    if other.search(clause) and not mine.search(clause):
        raise text.UnreadableError(f"{clause.strip()!r} does not state a {name}")


def figure(item: Mapping, name: str, edition: str | None, places: list[Place], taken: Mapping[Place, str], *,
           editions: list[str], protected: list[str], now: datetime, zone: str | None) -> tuple[Any, Place, Place]:
    """The amount or time of an item, at a place in the answers that no other item has taken."""
    money = name in ("value", "ceiling")
    wanted = text.amount(item.get("amount")) if money else None
    good, why = [], None
    for place in places:
        try:
            if money:
                found = [m for m in text.MONEY.finditer(place.answer)
                         if place.start <= m.start("num") and m.end("num") <= place.end
                         and wanted in [v for v, _ in text.amounts(m[0])]]
                if not found:
                    raise text.UnreadableError(f"{item.get('amount')!r} is not the figure in {places[0].said!r}")
            else:
                value = text.moment(place.said, place.answer, now, zone, place.span)
                found = text.times(place.answer, *place.span)
        except text.UnreadableError as exc:
            why = exc
            continue
        for m in found:
            spot = Place(place.question, place.answer, *m.span())
            try:
                if money:
                    value = wanted
                    text.check_amount(place.answer, m, editions)
                    figure_field(spot, name)
                    if name == "value":
                        value_edition(spot, edition, editions, protected)
            except text.UnreadableError as exc:
                why = exc
            else:
                good.append((value, place, spot))
    for found_at in good:
        if found_at[2] not in taken:
            return found_at
    for found_at in good:
        if taken[found_at[2]] == name:      # the same item again
            return found_at
    raise why or text.UnreadableError(f"the figure in {places[0].said!r} already belongs to another item")


def trade_off(place: Place) -> None:
    answer = place.answer
    first, last = text.around(answer, place.start, place.end, text.SENTENCE)
    if turns := list(TURN.finditer(answer[first:place.start])):
        first += turns[-1].end()        # "don't bankrupt me, but goodwill can suffer"
    if REFUSAL.search(answer[first:text.around(answer, place.start, place.end)[1]]):
        raise text.UnreadableError(f"{place.said!r} refuses a trade-off, it does not accept one")
    if answer[last:last + 1] == "?" or not ACCEPTS.search(place.said):
        raise text.UnreadableError(f"{place.said!r} does not say that something may suffer")


def unread(answers: Mapping[str, str], editions: list[str], figures: Iterable[Place],
           statements: Iterable[Place]) -> list[str]:
    """Amounts and numbers, in digits or words, that no item accounts for. Each place counts for itself."""
    figures, statements = list(figures), list(statements)
    out = []
    for key, answer in answers.items():
        done = [p.span for p in figures if p.question == key]
        done += [span for e in editions for span in text.spans(e, answer)]
        inside = lambda m: any(a <= m.start() and m.end() <= b for a, b in done)      # noqa: E731

        for m in text.MONEY.finditer(answer):
            if not inside(m) and (m["pre"] or m["post"] or m["scale"]):
                for value, _ in text.amounts(m[0]):
                    out.append(f"the amount {value:,} is in the answers and in no value or ceiling item")
                    done.append(m.span())
        for m in re.finditer(r"\d+(?:[.,:]\d+)*", answer):
            if inside(m):
                continue
            quoted = any(p.covers(key, *m.span()) for p in statements)
            if not (_in_a_name(answer, m, editions) or (quoted and _harmless(answer, m, editions))):
                out.append(f"the number {m[0]!r} is in the answers and in no item")
        for m in NUMBER_WORD.finditer(answer):
            if not inside(m) and not _harmless(answer, m, editions):
                out.append(f"the number {m[0]!r} is in the answers in words: give it in digits")
        for m in EURO_WORD.finditer(answer):
            if not inside(m):
                out.append(f"{m[0]!r} is in the answers without a figure code can read")
        for m in NO_MONEY.finditer(answer):
            out.append(f"{m[0]!r} is a figure: give it in digits")
    return list(dict.fromkeys(out))


def _in_a_name(answer: str, number: re.Match, editions: list[str]) -> bool:
    before = re.search(r"([^\W\d_]+)\s*$", answer[:number.start()])
    return bool(before) and any(number[0] in re.findall(r"\d+", e) and text.words(before[1]) & text.words(e)
                                for e in editions)


def _harmless(answer: str, number: re.Match, editions: list[str]) -> bool:
    """A count of things or the time of a deadline, where neither money nor the team is the subject."""
    first, last = text.around(answer, *number.span())
    if MONEY_TALK.search(answer[first:last]) or TEAM.search(answer[first:last]):
        return False
    if any(m.start() <= number.start() and number.end() <= m.end()
           for pattern in (text.ISO, text.CLOCK) for m in pattern.finditer(answer)):
        return number.re is not NUMBER_WORD or number[0].lower() == "noon"
    names = sorted({w for e in editions for w in re.findall(r"[^\W\d_]+", e)}, key=len, reverse=True)
    between = rf"(?:(?:{'|'.join(map(re.escape, names))})(?:'s)?\s+){{0,3}}" if names else ""
    return bool(re.match(rf"\s*%|\s+{between}{COUNTED}", answer[number.end():], re.IGNORECASE))


def unprotected(answers: Mapping[str, str], editions: list[str], lines: list[dict]) -> list[str]:
    """Editions the goal names that no priority protects: a value without an edition would spread to all."""
    protected = {x["edition"] for x in lines if x["field"] == "priority"}
    if "all" in protected:
        return []
    rest = answers.get("goal", "")
    for x in lines:
        if x["field"] in ("value", "trade_off") and x.get("question") == "goal":
            for start, end in reversed(text.spans(x["quote"], rest)):
                rest = rest[:start] + " . " + rest[end:]
    return [f"the goal names {e!r} and no priority item does" for e in named(rest, editions) if e not in protected]


def contradicted(answers: Mapping[str, str], lines: list[dict], filled: list[dict]) -> list[str]:
    """Pre-filled inputs that the typed answers talk about without an item that could replace them."""
    typed = {x["field"] for x in lines}
    out = []
    offered = [x["value"] for x in filled if x["field"] == "trade_off"]
    if offered and "trade_off" not in typed:
        rest = "\n".join(answers.values())
        for x in lines:
            if x["field"] not in ("priority", "trade_off"):
                for start, end in reversed(text.spans(x["quote"], rest)):
                    rest = rest[:start] + " . " + rest[end:]
        refuses, said = REFUSAL.search(rest), text.words(rest)
        for what in offered:
            if refuses or any(len(w) > 3 and w not in COMMON_WORDS for w in said & text.words(what)):
                out.append(f"the answers may contradict the pre-filled trade-off {what!r}: say what may suffer")
    if any(x["field"].startswith("team_") for x in filled) and not typed & {"team_from", "team_until"}:
        for sentence in text.SENTENCE.split("\n".join(answers.values())):
            if TEAM.search(sentence) and (WHEN.search(sentence) or text.DAY.search(sentence)):
                out.append(f"{sentence.strip()!r} may set the team window: give its time")
    return out
