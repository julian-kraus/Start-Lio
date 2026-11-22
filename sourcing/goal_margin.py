"""Does the goal protect margin? A closed grammar over the person's words. Pure functions, no LLM.

`read` gives (line, None) when it does, (None, None) when it does not and (None, words) when the words on margin
are not among the recognised forms. Those are never guessed: the goal's `protect` list settles them. The same
holds in every field: a sacrifice, a protect entry and the answer to a question on margin.

Once margin is named, the statement and all answers are read as one: each sentence and clause must be a margin form
or a known aside (on time, what may suffer, costs, limits, when the team can be asked), because a negation or a
condition can sit in any of them. An answer is not read apart from a question that turns it around.
"""

from __future__ import annotations

import re
from typing import Iterable, Mapping

MARGIN = re.compile(r"\bmargins?\b")
OTHER_MARGIN = re.compile(r"\b(?:safety|(?<!on )time) margins?\b|\bmargins? (?:of|for) (?:error|safety)\b")
M = r"(?:(?:the|our|my|their|gross|profit|net|operating) )*margins?"
V = r"(?:protect|protecting|preserve|preserving|defend|defending|safeguard|safeguarding)"
HARM = r"(?:sacrifice|hurt|erode|risk)"
CAN = r"(?:can|could|may|might)"
MUST = r"(?:must|should|has to|have to|needs to|need to)"
LEAD = (r"(?:(?:we|i|you|please|and|also|always|just|first|above all|must|should|need to|have to|want to|will|"
        r"it is important to|the goal is to|our priority is to) )*")
TAIL = r"(?: (?:first|above all|at all costs|too|as well|please))?"
PROTECTS = re.compile("|".join((
    rf"{LEAD}{V} {M}{TAIL}",
    rf"{LEAD}(?:do not|never|must not|cannot) (?:{HARM} {M}|let {M} (?:suffer|slip))",
    rf"(?:{M} protection|protection of {M})(?: is| comes)? (?:first|key)",
    rf"{M} (?:{MUST} be (?:protected|preserved)|is protected|is key|comes first|first|matters(?: most)?)",
    rf"{M} (?:cannot|must not|may not|should not) (?:suffer|slip|shrink|drop)",
)))
NOT = (r"(?:cannot|will not|do not|does not|must not|should not|need not|never|stop|forget(?: about)?|"
       r"no need to|not)")
# An aside takes no free words: an edition is one the goal itself names, what may suffer is goodwill or an edition
EDITION = r"<edition>(?: editions?)?(?: cop(?:y|ies))?"
COPIES = rf"(?:{EDITION}|(?:first )?(?:cop(?:y|ies)|editions?|orders?))"
ON_TIME = rf"(?:(?:all|every|each)(?: {COPIES})? |everything )?(?:on time|by the deadline)"
MAY_SUFFER = rf"(?:(?:the|our) )?(?:(?:supplier|customer) )?goodwill|(?:the )?{EDITION}"
SUM = r"(?:eur |\u20ac ?)?\d+(?:[., ]\d+)*k?(?: ?(?:eur|euros?|\u20ac))?"
DAY = r"(?:today|tomorrow|tonight|(?:mon|tues|wednes|thurs|fri|satur|sun)day)"
WHEN = rf"(?:(?:{DAY} )?\d\d?(?::\d\d)?(?: ?(?:am|pm))?|{DAY})"
ASIDE = re.compile("|".join((
    ON_TIME,
    r"(?:do not|never|must not|cannot) (?:bankrupt (?:me|us)|overspend|miss (?:the |our )?(?:date|deadline))",
    rf"(?:{MAY_SUFFER}) {CAN} (?:suffer|wait|slip)",
    r"please|above all|at all costs",
    rf"(?:a|one|each|every) (?:(?:missing|late) (?:copy|one)|copy) (?:costs|is worth) {SUM}",
    rf"(?:a|one) (?:missing|late) one {SUM}",
    rf"spend (?:at most|up to|no more than) {SUM}",
    rf"(?:the )?team (?:can be asked|is available|is offline|is online|offline) (?:until|from|at) {WHEN}",
    r"\d{4}-\d\d-\d\d[t ]\d\d:\d\d(?::\d\d)?(?:[+-]\d\d:\d\d|z)?",      # a stored team window
)))
GIVES_UP = re.compile("|".join((
    rf"{LEAD}{NOT} (?:(?:need to|have to|try to|want to) )?{V} {M}{TAIL}",
    rf"{LEAD}(?:do not|does not) (?:care about|mind) {M}",
    rf"(?:{ON_TIME} )?(?:rather than|instead of) {V} {M}",
    rf"(?:{M} protection|protection of {M}|{M}) {CAN} (?:suffer|wait|slip|shrink|drop|give)",
    rf"{M} (?:(?:does|do) not matter|is not (?:important|a priority))",
)))
NEGATION = re.compile(r"\b(?:not|never|cannot|no|stop|forget)\b")
TURNS = re.compile(rf"{NEGATION.pattern}|\b(?:least|avoid|give up|giving up|skip)\b"
                   r"|^(?:what|which)\b[^?]*\b(?:sacrific\w*|suffer|wait|slip|drop)\b")
CONDITION = re.compile(r"\b(?:unless|except|only if|only when|as long as)\b")
GIVEN = re.compile(rf"(?:(?:some|lower|a bit of|a little) )?{M}|{GIVES_UP.pattern}")
KEPT = re.compile(rf"{M}|{PROTECTS.pattern}")
ASKS = re.compile(rf"(?:(?:should|do|must) (?:we|i|you) (?:(?:want to|need to|have to) )?)?{V} {M}")
NO = re.compile(r"no|nope|false")
SENTENCE = re.compile(r"(?<=[!?\n])|(?<=\.)(?!\d)")
CLAUSE = re.compile(r";|[,:](?!\d)|\s[-\u2013]+\s|\u2014|(?<!anything )(?<!everything )(?<!nothing )\bbut\b",
                    re.IGNORECASE)


def _strings(value) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        # a question or its id is not what the goal says
        for key, item in value.items():
            if key not in ("id", "q", "question"):
                yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _entries(value) -> Iterable[str]:
    """The entries of a protect or sacrifice field. A key on margin is shown with its value, so it is never dropped."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            if MARGIN.search(str(key).lower().replace("_", " ")):
                yield f"{str(key).replace('_', ' ')}: {item}"
            elif key not in ("q", "question"):
                yield from _entries(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _entries(item)
    else:
        yield from _strings(value)


def _plain(text: str) -> str:
    text = " ".join(text.lower().replace("\u2019", "'").split())
    for short, full in (("can't", "cannot"), ("can not", "cannot"), ("won't", "will not"), ("n't", " not"),
                        ("dont ", "do not ")):
        text = text.replace(short, full)
    return OTHER_MARGIN.sub("", text).strip(" \"'")


def _asked(answers) -> Iterable[tuple[str, object]]:
    """Each question, or the key standing for one, with its answer."""
    if isinstance(answers, (list, tuple)):
        for item in answers:
            yield from _asked(item)
    elif isinstance(answers, Mapping):
        if question := answers.get("q") or answers.get("question"):
            yield str(question), answers.get("a", answers.get("answer"))
        else:
            for key, item in answers.items():
                if isinstance(item, (Mapping, list, tuple)):
                    yield from _asked(item)
                else:
                    yield str(key).replace("_", " "), item


def _editions(goal: Mapping) -> re.Pattern | None:
    """The edition names in the goal's own fields, as they read after `_plain`."""
    found = [goal.get("edition")]
    if isinstance(prices := goal.get("unit_loss_by_edition"), Mapping):
        found += prices
    found += [x.get("edition") for x in goal.get("goal_lines") or [] if isinstance(x, Mapping)]
    names = {name for x in found if x and (name := _plain(str(x))) not in ("", "all", "both")}
    if not names:
        return None
    either = "|".join(map(re.escape, sorted(names, key=len, reverse=True)))
    return re.compile(rf"(?<![\w'-])(?:{either})(?![\w'-])")


def read(goal: Mapping | None) -> tuple[str | None, str | None]:
    if not goal:
        return None, None
    given, unclear = False, None
    for text in _entries(goal.get("sacrifices")):
        if MARGIN.search(said := _plain(text).rstrip(" .!;")):
            if GIVEN.fullmatch(said):
                given = True
            else:
                unclear = unclear or text
    if given and not unclear:
        return None, None
    if goal.get("protect") is not None:
        kept = None
        for text in _entries(goal["protect"]):
            if MARGIN.search(said := _plain(text).rstrip(" .!;")):
                if not KEPT.fullmatch(said):
                    return None, text
                kept = kept or f"protect: {text}"
        return kept, None
    if unclear:
        return None, unclear

    protects, gives_up = [], []
    for question, answer in _asked(goal.get("answers")):
        asked, said = _plain(question).rstrip(" ?"), _plain("" if answer is None else str(answer)).rstrip(" .!")
        if said and MARGIN.search(asked) and not MARGIN.search(said):       # "yes" alone does not mention margin
            if not (ASKS.fullmatch(asked) and NO.fullmatch(said)):
                return None, f"{question} {answer}"
            gives_up.append(f"{question} {answer}")
        elif MARGIN.search(said) and TURNS.search(asked):
            return None, f"{question} {answer}"
    texts = list(_strings([goal.get("statement"), goal.get("answers")]))
    if not any(MARGIN.search(_plain(line)) for text in texts for line in SENTENCE.split(text)):
        return None, None
    editions = _editions(goal)
    for text in texts:
        for sentence in SENTENCE.split(text):
            whole = _plain(sentence)
            if MARGIN.search(whole) and (whole.endswith("?") or CONDITION.search(whole)):
                return None, sentence.strip()
            for clause in CLAUSE.split(sentence.strip().rstrip(".;!")):
                negated = False     # "we cannot hit the date and protect margin" has an open scope
                for part in re.split(r"\band\b", clause, flags=re.IGNORECASE):
                    said = _plain(part)
                    if MARGIN.search(said):
                        if GIVES_UP.fullmatch(said):
                            gives_up.append(part.strip())
                        elif PROTECTS.fullmatch(said) and not negated:
                            protects.append(part.strip())
                        else:
                            return None, sentence.strip()
                    elif said:
                        aside = editions.sub("<edition>", said) if editions else said
                        if not ASIDE.fullmatch(aside):
                            return None, sentence.strip()
                    negated = negated or bool(NEGATION.search(said))
    if protects and gives_up:
        return None, f"{protects[0]} / {gives_up[0]}"
    return (protects[0] if protects else None), None
