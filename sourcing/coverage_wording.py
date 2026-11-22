"""Rules that kb has only as wording: phantom lines and the priority between editions.

Wording is the weakest evidence, so it fails towards a visible problem: a phantom note with any word that is
not understood leaves the shortage standing, and so does a doubt. A priority counts only when nothing
unexplained follows it or is said about another edition. Both escalate.
"""

from __future__ import annotations

import re

NOT_STOCKED = re.compile(r"\b(?:not|never)\s+(?:(?:ordered|bought|purchased|received|delivered|kept|or|and|nor)\b"
                         r"[\s,]*)*stocked (?:separately|on (?:its|their) own)\b", re.I)
PHANTOM = re.compile(r"\bphantom\b|\b(?:pre-?)?(?:fitted|mounted|assembled|installed) by (?:the |our )?"
                     r"(?:supplier|vendor)\b", re.I)
DOUBT = re.compile(r"\b(?:no|not|never|non|nor|cannot|without|false|former|formerly|was|were|longer|until|used to"
                   r"|should|if|whether|unless|maybe|possibly|isnt)\b|n['\u2019]t\b|\?"
                   # taken back, called a mistake, or contradicted by an instruction to buy
                   r"|\b(?:withdr[ae]w|remov|cancel|ceas|revok|rescind|revers|retract|lifted|dropped|obsolete|ended|expired"
                   r"|error|mistak|wrong|incorrect|disput|previous|earlier)\w*"
                   r"|\b(?:buy|reorder|order (?:it|them|separately)|(?:must|to) be (?:ordered|bought))\b", re.I)
WORD = re.compile(r"[^\W\d_]+")
# all a phantom note may say, besides the names of the parts
PHANTOM_WORDS = frozenset("""a an the and or is are as it its this that part parts line lines bom item component
    phantom pre prefitted fitted premounted mounted preassembled assembled preinstalled installed by with in into
    on onto to of our supplier vendor""".split())
# all that may follow a priority, or be said about an edition that is not first
PRIORITY_WORDS = frozenset("""a an the and for of on in to is are be s goes go gets get edition editions shared
    part parts first then second last waits wait followed by""".split())
STALE = re.compile(r"\b(?:old|stale|void|outdated|at first)\b|\b(?:supersed|suspend|replac)")
SENTENCE = re.compile(r"(?<=[.;!?\n])")
CLAUSE = re.compile(r"(?<=[,;.:!?\n])|\bthen\b|\bfollowed by\b")
NEXT = re.compile(r"\bthen\b|\bfollowed by\b")
BEHIND = re.compile(r"\b(?:waits?|last|second|after|until)\b")
TOKEN = re.compile(r"@(\d+)@")


def phantom(note: str, part_names: frozenset[str] = frozenset()) -> bool | None:
    """True: all the note says is phantom. False: it mentions one and says more, or doubts it. None: no mention."""
    rests, said = [], False
    for sentence in SENTENCE.split(note):
        rest = NOT_STOCKED.sub(" ", sentence)
        said = said or rest != sentence or bool(PHANTOM.search(rest))
        rests.append(rest)
    if not said:
        return None
    known = PHANTOM_WORDS | part_names
    return not any(DOUBT.search(r) or set(WORD.findall(r.lower())) - known for r in rests)


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
    if STALE.search(masked):
        return True, None
    leaders = set()
    for clause in CLAUSE.split(masked):
        understood = set(WORD.findall(clause)) <= PRIORITY_WORDS
        if not re.search(r"\bfirst\b", clause):
            if not understood and (named(clause) or leaders):
                return True, None                       # it may take the priority back, or hand it on
            continue
        who = named(clause)
        before, after = re.split(r"\bfirst\b", clause, maxsplit=1)
        if len(who) != 1 or DOUBT.search(clause) or BEHIND.search(clause) or not named(before) \
                or set(WORD.findall(after)) - PRIORITY_WORDS:
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
