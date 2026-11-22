"""Rules that kb has only as wording: phantom lines and the priority between editions.

Wording is the weakest evidence, so it fails towards a visible problem: a negated or doubtful phantom note
leaves the shortage standing, and an unclear priority is no priority. Both escalate.
"""

from __future__ import annotations

import re

NOT_STOCKED = re.compile(r"\b(?:not|never)\s+(?:(?:ordered|bought|purchased|received|delivered|kept|or|and|nor)\b"
                         r"[\s,]*)*stocked (?:separately|on (?:its|their) own)\b", re.I)
PHANTOM = re.compile(r"\bphantom\b|\b(?:pre-?)?(?:fitted|mounted|assembled|installed) by (?:the |our )?"
                     r"(?:supplier|vendor)\b", re.I)
DOUBT = re.compile(r"\b(?:no|not|never|non|nor|cannot|without|false|former|formerly|was|were|longer|until|used to"
                   r"|should|if|whether|unless|maybe|possibly|isnt)\b|n['\u2019]t\b|\?", re.I)
SENTENCE = re.compile(r"(?<=[.;!?\n])")
CLAUSE = re.compile(r"(?<=[,;.:!?\n])|\bthen\b|\bfollowed by\b")
NEXT = re.compile(r"\bthen\b|\bfollowed by\b")
BEHIND = re.compile(r"\b(?:waits?|last|second|after|until)\b")
TOKEN = re.compile(r"@(\d+)@")


def phantom(note: str) -> bool | None:
    """True: the note says phantom. False: it mentions one with a negation or doubt. None: no mention."""
    said = None
    for sentence in SENTENCE.split(note):
        rest = NOT_STOCKED.sub(" ", sentence)
        if not PHANTOM.search(rest) and rest == sentence:
            continue
        if DOUBT.search(rest):
            return False
        said = True
    return said


def priority(text: str, labels: dict[str, list[str]]) -> tuple[bool, list[str] | None]:
    """(names an edition, product ids in the order the text states, or None when it does not state one)."""
    products, masked = list(labels), text.lower()
    names = sorted(((label.lower(), n) for n, p in enumerate(products) for label in labels[p] if label),
                   key=lambda item: (-len(item[0]), item))
    for label, n in names:                              # longest first, so a name inside a name is not a hit
        masked = re.sub(rf"(?<![\w@]){re.escape(label)}(?![\w@])", f" @{n}@ ", masked)

    def named(part: str) -> set[str]:
        return {products[int(n)] for n in TOKEN.findall(part)}

    if not named(masked):
        return False, None
    leaders = set()
    for clause in CLAUSE.split(masked):
        if not re.search(r"\bfirst\b", clause):
            continue
        who = named(clause)
        if len(who) != 1 or DOUBT.search(clause) or BEHIND.search(clause):
            return True, None
        leaders |= who
    if len(leaders) != 1:
        return True, None
    order = [*leaders, *(p for p in products if p not in leaders)]
    if len(products) < 3:
        return True, order
    steps = [named(step) for step in NEXT.split(masked)]
    stated = [p for step in steps if len(step) == 1 for p in step]
    whole = len(steps) == len(products) == len(set(stated)) and stated[0] in leaders
    return True, stated if whole else None
