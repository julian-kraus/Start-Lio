"""Does the goal protect margin? A closed grammar over the person's words. Pure functions, no LLM.

`read` gives (line, None) when it does, (None, None) when it does not and (None, words) when the words on margin
are not among the recognised forms. Those are never guessed: the goal's `protect` list settles them.
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
GIVES_UP = re.compile("|".join((
    rf"{LEAD}{NOT} (?:(?:need to|have to|try to|want to) )?{V} {M}{TAIL}",
    rf"{LEAD}(?:do not|does not) (?:care about|mind) {M}",
    rf"(?:.+ )?(?:rather than|instead of) {V} {M}",
    rf"(?:{M} protection|protection of {M}|{M}) {CAN} (?:suffer|wait|slip|shrink|drop|give)",
    rf"{M} (?:(?:does|do) not matter|is not (?:important|a priority))",
)))
NEGATION = re.compile(r"\b(?:not|never|cannot|no|stop|forget)\b")
CONDITION = re.compile(r"\b(?:unless|except|only if|only when|as long as)\b")
ALL_BUT = re.compile(r"\b(?:except|other than|apart from|but|not)\b")
CLAUSE = re.compile(r"[,:]|\s[-–]+\s|—|(?<!anything )(?<!everything )(?<!nothing )\bbut\b", re.IGNORECASE)


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


def _plain(text: str) -> str:
    text = " ".join(text.lower().replace("’", "'").split())
    for short, full in (("can't", "cannot"), ("can not", "cannot"), ("won't", "will not"), ("n't", " not"),
                        ("dont ", "do not ")):
        text = text.replace(short, full)
    return OTHER_MARGIN.sub("", text).strip(" \"'")


def read(goal: Mapping | None) -> tuple[str | None, str | None]:
    if not goal:
        return None, None
    for text in _strings(goal.get("sacrifices")):
        if MARGIN.search(said := _plain(text)):
            return (None, text) if ALL_BUT.search(said) else (None, None)
    if goal.get("protect") is not None:
        kept = [text for text in _strings(goal["protect"]) if "margin" in text.lower()]
        return (f"protect: {kept[0]}" if kept else None), None

    protects, gives_up = [], []
    for text in _strings([goal.get("statement"), goal.get("answers")]):
        for sentence in re.findall(r"[^.;!?\n]+[.;!?]?", text):
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
                            return None, clause.strip()
                    negated = negated or bool(NEGATION.search(said))
    if protects and gives_up:
        return None, f"{protects[0]} / {gives_up[0]}"
    return (protects[0] if protects else None), None
