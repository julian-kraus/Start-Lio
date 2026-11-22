"""Does the goal protect margin? A closed grammar over the person's words. Pure functions, no LLM.

`read` gives (line, None) when it does, (None, None) when it does not and (None, words) when the words on margin
are not among the recognised forms. Those are never guessed: the goal's `protect` list settles them. The same
holds in every field: a sacrifice, a protect entry and the answer to a question on margin.

A sentence on margin, semicolons included, is read as a whole: each other part of it must be a known aside,
because a negation or a condition can sit in any clause. An answer is not read apart from a question that turns
it around. Not seen: a negation or condition in a separate sentence, other than a short withdrawal such as "Not."
"""

from __future__ import annotations

import re
from typing import Iterable, Mapping

MARGIN = re.compile(r"\bmargins?\b")
OTHER_MARGIN = re.compile(r"\b(?:safety|time) margins?\b|\bmargins? (?:of|for) (?:error|safety)\b")
M = r"(?:(?:the|our|my|their|gross|profit|net|operating) )*margins?"
V = r"(?:protect|protecting|preserve|preserving|defend|defending|safeguard|safeguarding)"
HARM = r"(?:sacrifice|hurt|erode|risk)"
CAN = r"(?:can|could|may|might)"
MUST = r"(?:must|should|has to|have to|needs to|need to)"
LEAD = (r"(?:(?:we|i|you|please|and|also|always|just|first|above all|must|should|need to|have to|want to|will|"
        r"it is important to|the goal is to|our priority is to) )*")
TAIL = r"(?: (?:first|above all|at all costs|too|as well|please|where possible))?"
PROTECTS = re.compile("|".join((
    rf"{LEAD}{V} {M}{TAIL}",
    rf"{LEAD}(?:do not|never|must not|cannot) (?:{HARM} {M}|let {M} (?:suffer|slip))",
    rf"(?:{M} protection|protection of {M})(?: is| comes)? (?:first|key)",
    rf"{M} (?:{MUST} be (?:protected|preserved)|is protected|is key|comes first|first|matters(?: most)?)",
    rf"{M} (?:cannot|must not|may not|should not) (?:suffer|slip|shrink|drop)",
)))
NOT = (r"(?:cannot|will not|do not|does not|must not|should not|need not|never|stop|forget(?: about)?|"
       r"no need to|not)")
# A word of an aside: none that negates, limits or points back at margin
WORD = (r"(?!(?:not|no|never|nor|if|when|once|only|unless|it|this|that|which|protect\w*|margins?)\b)"
        r"[\w'-]+")
ON_TIME = rf"(?:(?:all|every|each)(?: {WORD}){{0,3}} |everything )?(?:on time|by the deadline)"
ASIDE = re.compile("|".join((
    ON_TIME,
    r"(?:do not|never|must not|cannot) (?:bankrupt (?:me|us)|overspend|miss (?:the |our )?(?:date|deadline))",
    rf"{WORD}(?: {WORD}){{0,2}} {CAN} (?:suffer|wait|slip)",
    r"please|above all|at all costs",
)))
GIVES_UP = re.compile("|".join((
    rf"{LEAD}{NOT} (?:(?:need to|have to|try to|want to) )?{V} {M}{TAIL}",
    rf"{LEAD}(?:do not|does not) (?:care about|mind) {M}",
    rf"(?:{ON_TIME} )?(?:rather than|instead of) {V} {M}",
    rf"(?:{M} protection|protection of {M}|{M}) {CAN} (?:suffer|wait|slip|shrink|drop|give)",
    rf"{M} (?:(?:does|do) not matter|is not (?:important|a priority))",
)))
NEGATION = re.compile(r"\b(?:not|never|cannot|no|stop|forget)\b")
WITHDRAWN = re.compile(r"(?:(?:actually|well|or|just) )?(?:not|no|nope|never|do not|kidding|never mind|scratch that"
                       r"|forget (?:it|that))")
TURNS = re.compile(rf"{NEGATION.pattern}|\b(?:least|avoid|give up|giving up|skip)\b")
CONDITION = re.compile(r"\b(?:unless|except|only if|only when|as long as)\b")
GIVEN = re.compile(rf"(?:(?:some|lower|a bit of|a little) )?{M}|{GIVES_UP.pattern}")
KEPT = re.compile(rf"{M}|{PROTECTS.pattern}")
ASKS = re.compile(rf"(?:(?:should|do|must) (?:we|i|you) (?:(?:want to|need to|have to) )?)?{V} {M}")
NO = re.compile(r"no|nope|false")
CLAUSE = re.compile(r"[,:;]|\s[-\u2013]+\s|\u2014|(?<!anything )(?<!everything )(?<!nothing )\bbut\b", re.IGNORECASE)


def _strings(value) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        # a question is not what the goal says
        for key, item in value.items():
            if key not in ("q", "question"):
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
    for text in _strings([goal.get("statement"), goal.get("answers")]):
        sentences = re.findall(r"[^.!?\n]+[.!?]?", text)
        if MARGIN.search(_plain(text)) and any(WITHDRAWN.fullmatch(_plain(x).rstrip(" .;!")) for x in sentences):
            return None, text.strip()
        for sentence in sentences:
            whole = _plain(sentence)
            if not MARGIN.search(whole):
                continue
            if whole.endswith("?") or CONDITION.search(whole):
                return None, sentence.strip()
            for clause in CLAUSE.split(sentence.rstrip(".;!")):
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
                    elif said and not ASIDE.fullmatch(said):
                        return None, sentence.strip()
                    negated = negated or bool(NEGATION.search(said))
    if protects and gives_up:
        return None, f"{protects[0]} / {gives_up[0]}"
    return (protects[0] if protects else None), None
