"""Read who holds which seat out of an org chart document.

A chart prints a name above its title, or `Name (Title)` in a list. A line is accepted as a holder only if it has
the shape of a person's name and nothing around it says otherwise. Every other holder is `unclear`, so routing
asks a human. The word lists below only take names away, they never make one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from sourcing.approval_docs import Document, iso_date, words

DATED = re.compile(r"\s*\((from|as of|starting|until|to)\s+(\d{4}-\d\d-\d\d)\)", re.I)
VACANT = {"tbd", "tba", "tbc", "vacant", "open", "open position", "position open", "n.n.", "nn", "n/a", "-", "?",
          "\u2013", "\u2014", "vacancy", "unassigned", "unfilled", "n.n"}
VACANT_PHRASE = re.compile(
    r"to be \w+|not yet \w+|(?:vacant|open) (?:position|role|post|seat)|(?:position|role|post|seat) (?:vacant|open)")
SENIORITY = {"senior", "junior", "principal", "acting"}

# The shape of a name: an optional honorific, then two to four capitalised words, with particles between them.
HONORIFIC = re.compile(r"(?:(?:Dr|Prof|Mr|Mrs|Ms|Mx|Ir|Ing)\.\s*)+")
INITIALS = re.compile(r"(?:[^\W\d_]\.)+")
PARTICLES = {"van", "von", "de", "der", "den", "del", "della", "di", "da", "das", "dos", "do", "du", "la", "le",
             "lo", "ten", "ter", "te", "el", "al", "bin", "binti", "ibn", "zu", "af", "av"}
# Capitalised words that pass the shape and still are not a person: a state of the seat ...
ABSENT = {"leave", "absent", "absence", "sick", "maternity", "paternity", "parental", "sabbatical", "secondment",
          "suspended", "retired", "departed", "resigned"}
NOT_A_NAME = ABSENT | {
    "tbd", "tba", "tbc", "vacant", "vacancy", "unassigned", "unfilled", "interim", "acting", "recruiting",
    "recruitment", "pending", "none", "nobody", "unknown", "position", "role", "appointed", "hiring", "on", "until",
    "till", "from", "since", "through", "starting", "starts", "and", "or", "of", "for", "with", "the", "deputy",
    "designate", "delegate", "substitute", "outgoing", "incoming", "temporary", "temporarily", "cover", "covering"}
# ... or a unit or a job, as printed in a heading over an empty seat.
NOT_A_PERSON = set("""
    administration admin analyst assistant assurance board bureau business centre center chair commercial committee company
    compliance control controller controlling coordination coordinator corporate council customer department depot
    desk development director directors directorate distribution division engineer engineering executive facilities
    finance financial function general global group headquarters human inbound lead leader leadership legal
    logistics maintenance management manager managing manufacturing marketing office officer officers operations
    organisation organization outbound owner personnel planner planning plant procurement production products
    programme program project purchasing quality regional representative research resources sales secretary section
    service services shared site specialist staff strategy supervisor supply support team technical technology
    treasury unit warehouse works
""".split())


@dataclass
class Seat:
    title: str
    name: str | None
    state: str                     # held | vacant | unclear
    start: date | None
    end: date | None
    ids: list[str]


@dataclass
class Chart:
    path: str
    version: str | None
    dated: date | None
    problems: list[str]
    ids: list[str]
    seats: list[Seat] = field(default_factory=list)


def title_scope(title: str, term: str) -> str | None:
    """None when the title is not this role; otherwise what the title is limited to, "" for the whole company."""
    paren = re.fullmatch(r"(.*?)\s*\((.*)\)", title.strip())
    base = words(paren[1] if paren else title)
    while base and base[0] in SENIORITY:
        base = base[1:]
    wanted = words(term)
    if re.fullmatch(r"C[A-Z]O", term.strip()):
        if len(base) < 3 or base[0] != "chief" or base[2] != "officer" or base[1][0] != term.strip()[1].lower():
            return None
        size = 3
    elif wanted and base[:len(wanted)] == wanted:
        size = len(wanted)
    else:
        return None
    return " ".join(base[size:] + (words(paren[2]) if paren else []))


def is_vacant(text: str) -> bool:
    name = DATED.sub("", text).strip().casefold()
    plain = name.rstrip(".")
    return name in VACANT or plain in VACANT or bool(VACANT_PHRASE.fullmatch(plain))


def _name_word(word: str) -> bool:
    if INITIALS.fullmatch(word):
        return word[0].isupper()
    parts = re.split(r"[-'\u2019]", word)
    for i, part in enumerate(parts):
        if not part.isalpha() or not part[0].isupper():
            return False
        if len(part) == 1:
            if i == len(parts) - 1:         # a lone capital is a name only before an apostrophe
                return False
        elif not part[1:].islower() and not re.fullmatch(r"[^\W\d_][a-z]{1,2}[A-Z][^\W\d_A-Z]+", part):
            return False
    return True


def is_name(text: str, title_words: set[str]) -> bool:
    """Whether the text is nothing but a person's name. `title_words` are the words of this chart's titles."""
    name = HONORIFIC.sub("", text.strip(), count=1) if HONORIFIC.match(text.strip()) else text.strip()
    parts = name.split()
    proper = [p for p in parts if p not in PARTICLES]
    if not 2 <= len(proper) <= 4 or len(parts) > 6 or parts[0] in PARTICLES or parts[-1] in PARTICLES:
        return False
    if INITIALS.fullmatch(parts[-1]) or not all(map(_name_word, proper)):
        return False
    used = {piece for word in words(name) for piece in (word, *word.split("'"))}
    return not used & (NOT_A_NAME | NOT_A_PERSON | title_words)


def _seat(title: str, person: str | None, ids: list[str], title_words: set[str]) -> Seat:
    unclear = Seat(title, None, "unclear", None, None, ids)
    if person is None:
        return unclear
    start = end = None
    for kind, when in DATED.findall(person):
        parsed = iso_date(when)
        if parsed is None:
            return unclear
        if kind.lower() in ("until", "to"):
            end = parsed
        else:
            start = parsed
    if is_vacant(person):
        return Seat(title, None, "vacant", None, None, ids)
    name = DATED.sub("", person).strip()
    return Seat(title, name, "held", start, end, ids) if is_name(name, title_words) else unclear


def _items(text: str) -> list[str]:
    """Split at the commas that are outside parentheses."""
    out, depth, start = [], 0, 0
    for i, char in enumerate(text):
        depth += (char == "(") - (char == ")")
        if char == "," and not depth:
            out.append(text[start:i].strip())
            start = i + 1
    return out + [text[start:].strip()]


def read_chart(doc: Document, terms: list[str]) -> Chart:
    header = None
    for line in doc.lines[:4]:
        header = re.search(r"\bv(?:ersion\s*)?(\d+(?:\.\d+)*)\b.*?(\d{4}-\d\d-\d\d)", line.text, re.I)
        if header:
            ids = [line.fact_id]
            break
    if not header or not iso_date(header[2]):
        return Chart(doc.path, None, None, doc.problems + [f"{doc.path}: no version and date"], [])
    chart = Chart(doc.path, header[1], iso_date(header[2]), list(doc.problems), ids)

    def is_title(text: str) -> bool:
        return len(text.split()) <= 10 and not text.endswith(".") and any(
            title_scope(text, term) is not None for term in terms)

    lines = doc.lines
    found = []                      # (title, its line, the holder where the list prints it on the same line)
    for i, line in enumerate(lines):
        listed = re.fullmatch(r"\w+:\s*(.+\(.+\))", line.text)
        if listed:
            for item in _items(listed[1]):
                pair = re.fullmatch(r"([^()]+?)\s*\((.+)\)", item)
                if pair and is_title(pair[2]):
                    found.append((pair[2], i, pair[1]))
        elif is_title(line.text):
            found.append((line.text, i, None))
    # what this chart calls a seat is nobody's name
    title_words = {w for text in [*terms, *(title for title, _, _ in found)] for w in words(text)
                   if len(w) > 2 and w not in PARTICLES}

    def qualified(i: int) -> bool:
        """The line below a title says something about the seat."""
        if i + 1 >= len(lines):
            return False
        below = lines[i + 1].text
        if is_vacant(below) and i + 2 < len(lines) and is_title(lines[i + 2].text):
            return False            # the holder of the next seat
        return not below[0].isupper() or bool(ABSENT & set(words(below))) or is_vacant(below)

    for title, i, person in found:
        cited = [lines[i].fact_id]
        if person is None:
            above = lines[i - 1] if i and lines[i - 1].fact_id not in ids else None
            if above:
                cited.append(above.fact_id)
                person = above.text
                # two names in a row: the second may be the title of a role the policy does not use
                if i > 1 and is_name(DATED.sub("", lines[i - 2].text), title_words):
                    person = None
            if qualified(i):
                person = None
                cited.append(lines[i + 1].fact_id)
        chart.seats.append(_seat(title, person, cited, title_words))
    return chart
