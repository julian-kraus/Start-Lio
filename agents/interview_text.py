"""Read amounts, times and names out of the words of a goal answer. Pure functions, no model.

The interview model only points at words; everything that becomes a number or a time is parsed here, so a misread
figure cannot come from the model. What cannot be read is refused, never guessed.
"""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone
from typing import Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from plan import clock

MAX_EUR = 1e12      # beyond this it is a misread
SPACE = "[ \u00a0\u202f]"
EURO = r"(?:€|eur\b|euros?\b)"
SCALE = {"k": 1e3, "thousand": 1e3, "tsd": 1e3, "million": 1e6, "mio": 1e6}
MONEY = re.compile(
    # not inside a larger number, not negative, not another currency
    rf"(?<![\w.,$£\-−])(?<!\d{SPACE})(?<!usd )(?<!gbp )(?<!pln )(?P<pre>{EURO}\s*)?"
    rf"(?P<num>[1-9]\d{{0,2}}(?:(?:{SPACE}|[.,'])\d{{3}})+(?:[.,]\d{{1,2}})?|\d+(?:[.,]\d{{1,2}})?)"
    rf"(?!\d|[.,']\d|{SPACE}\d{{3}}(?!\d))"
    rf"(?:\s*(?P<scale>k|thousand|tsd|million|mio)(?![^\W_]))?"
    rf"(?P<post>\s*{EURO})?"
    r"(?!\w|:\d|\s*(?:[%$£]|(?:percent|usd|gbp|pln|zł|m|bn|billion|mrd|k|thousand|tsd|million|mio|hundred)\b)"
    r"|\s*[-–]\s*\d)", re.IGNORECASE)
CLOCK = re.compile(r"(?<![\d:])(?:(?P<h>[01]?\d|2[0-3]):(?P<m>[0-5]\d)(?:\s*(?P<ap>[ap]m\b|[ap]\.m\.?(?!\w)))?(?![\d:])"
                   r"|(?P<h12>1[0-2]|0?[1-9])\s*(?P<ap12>[ap]m\b|[ap]\.m\.?(?!\w))|(?P<noon>\bnoon\b))", re.IGNORECASE)
DAYS = {word: day for day, words in enumerate(("mon monday", "tue tues tuesday", "wed wednesday",
                                               "thu thur thurs thursday", "fri friday", "sat saturday",
                                               "sun sunday")) for word in words.split()}
RELATIVE = {"today": 0, "tonight": 0, "tomorrow": 1}
DAY = re.compile(r"\b(" + "|".join(sorted([*DAYS, *RELATIVE], key=len, reverse=True)) + r")\b", re.IGNORECASE)
ISO = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d)?(?:Z|[+-]\d\d:\d\d)", re.IGNORECASE)
END = r"(?<![ap]\.m)[.!?;](?=\s|$)|\n(?!(?:or|and|nor)\b)"
SENTENCE = re.compile(END, re.IGNORECASE)
PART = re.compile(END + r"|,\s|\s(?:and|but|while|whereas)(?=\s)", re.IGNORECASE)
PUNCT = r"[ ]*(?:$|\n|[.,;:!?)\"'”]+(?:\s|$))"
# what may follow a bare number; any other word could be a unit, a scale or a currency
AFTER_AMOUNT = re.compile(
    PUNCT + r"|\s*/\s*(?:copy|unit|piece|book)\b"
    r"|\s+(?:a|an|per|each|every|apiece|for|and|or|but|max|maximum|tops|total|overall|altogether|at|on|in|if|when"
    r"|while|to|until|by|is|as|copy|missing|lost)(?![\w-])", re.IGNORECASE)
SCALES = (r"\b(?:hundreds?|thousands|grand|millions|billions?|trillions?|bn|mrd|mln|mill|lakhs?|crores?|dozens?|half"
          r"|tausend|hundert|millionen|milliarden?|cents?)\b")
SCALE_WORD = re.compile(SCALES, re.IGNORECASE)
FOREIGN = re.compile(
    r"[$£¥]|zł|\b(?:dollars?|bucks|usd|chf|gbp|pln|sek|nok|dkk|czk|huf|jpy|cny|cad|aud|pounds?|sterling|quid|francs?"
    r"|franken|fr\.|zlot(?:y|ys|ych)|złot\w*|yen|yuan|kron(?:a|e|er|or)|crowns?|rupees?|rubles?|liras?)(?!\w)",
    re.IGNORECASE)
AFTER_TIME = re.compile(
    PUNCT + r"|\s+in the (?:morning|afternoon|evening)\b"
    r"|\s+(?:on|until|till|to|and|or|but|the|team|we|they|is|are|goes?|sharp|latest|at|from|local|offline|online"
    r"|onwards?|" + "|".join([*DAYS, *RELATIVE]) + r")(?![\w:])", re.IGNORECASE)
ZONE_NAME = re.compile(r"\b(?:UTC|GMT|CET|CEST|BST|EET|EEST|WET|MSK|IST|JST|[ECMP][SD]T)\b|\b(?:utc|gmt)\b")
OTHER_TIME = re.compile(r"(?<!\bon )(?<!\bin )(?<!\blocal )\btime\b", re.IGNORECASE)
OTHER_DAY = re.compile(
    r"\b(?:day after|day before|yesterday|next|following|weeks?|weekend|daily|every day|days"
    r"|january|february|april|june|july|august|september|october|november|december"
    r"|jan|feb|apr|jun|jul|aug|sept?|oct|nov|dec"
    r"|morgen|übermorgen|heute|montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonntag|abends?|nachmittags?)\b",
    re.IGNORECASE)
LATE = re.compile(r"\b(?:tonight|evening|afternoon|night)\b", re.IGNORECASE)
EARLY = re.compile(r"\bmorning\b", re.IGNORECASE)


class UnreadableError(ValueError):
    pass


def one_line(text) -> str:
    return " ".join(str(text if text is not None else "").replace("’", "'").split())


def plain(text) -> str:
    return one_line(text).casefold()


def words(text) -> set[str]:
    """Words for matching a name: lower case, no possessive or plural s."""
    found = (w.casefold() for w in re.findall(r"[^\W_]+", one_line(text)))
    return {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in found if w.casefold() != "s"}


def names_edition(text: str, edition: str, editions: Iterable[str]) -> bool:
    """Whether the text uses a word that sets this edition apart from the others."""
    others = set().union(*(words(e) for e in editions if e != edition))
    own = (words(edition) - others) or words(edition)
    return bool(own & words(text))


def _value(match: re.Match) -> float | int:
    num = match["num"].strip()
    decimals = re.search(r"[.,](\d{1,2})$", num)
    whole = re.sub(r"\D", "", num[:decimals.start()] if decimals else num)
    value = float(f"{whole}.{decimals[1]}" if decimals else whole) * SCALE.get((match["scale"] or "").lower(), 1)
    value = round(value, 2)
    if value > MAX_EUR:
        raise UnreadableError(f"{match[0]!r} is too large to be an amount")
    return int(value) if value == int(value) else value


def amount(text) -> float | int:
    """The euro amount written in `text`, which holds nothing but that amount."""
    said = one_line(text)
    match = MONEY.fullmatch(said)
    if not match:
        raise UnreadableError(f"cannot read {said!r} as a euro amount")
    return _value(match)


def amounts(text) -> list[tuple[float | int, bool]]:
    """Every amount in the text, and whether it is marked as money (euro sign or word, or a scale)."""
    out = []
    for m in MONEY.finditer(one_line(text)):
        try:
            out.append((_value(m), bool(m["pre"] or m["post"] or m["scale"])))
        except UnreadableError:
            continue        # too large to be an amount; the interview still reports the number as unread
    return out


def normal(answer) -> str:
    """An answer as the quotes are searched in it: single spaces, line breaks kept."""
    return "\n".join(one_line(line) for line in str(answer).splitlines())


def spans(quote: str, answer: str) -> list[tuple[int, int]]:
    """Every place where the answer says the quote."""
    wanted = r"\s+".join(re.escape(word) for word in one_line(quote).split())
    if not wanted:
        return []
    return [m.span() for m in re.finditer(rf"(?<!\w){wanted}(?!\w)", answer, re.IGNORECASE)]


def around(answer: str, start: int, end: int, limit: re.Pattern = PART) -> tuple[int, int]:
    """The part of the answer (by default the clause, with SENTENCE the sentence) around a place in it."""
    first, last = 0, len(answer)
    for m in limit.finditer(answer):
        if m.end() <= start:
            first = m.end()
        elif m.start() >= end:
            last = m.start()
            break
    return first, last


def check_amount(answer: str, match: re.Match, names: Iterable[str] = ()) -> None:
    """Refuse an amount that the answer gives in another unit, scale or currency, or with a sign."""
    said = repr(match[0].strip())
    first, last = around(answer, *match.span(), SENTENCE)
    rest = answer[first:match.start()] + " " + answer[match.end():last]
    if FOREIGN.search(rest) or SCALE_WORD.search(rest):
        raise UnreadableError(f"cannot tell the currency or scale of {said}")
    before, after = answer[:match.start()].rstrip(), answer[match.end():]
    if re.search(r"(?:[-−+~*×/]|\b(?:minus|negative))$", before, re.IGNORECASE):
        raise UnreadableError(f"cannot read the sign before {said}")
    if not (match["pre"] or match["post"]) and re.search(r"\b(?!EUR$)[A-Z]{3}$", before):
        raise UnreadableError(f"cannot tell the currency of {said}")
    if match["post"] or AFTER_AMOUNT.match(after):
        return
    follower = re.match(r"\s+([^\W\d_]+)", after)
    if follower and words(follower[1]) & set().union(*(words(n) for n in names)):
        return      # "75 Deluxe": the word after the number is an edition's name
    raise UnreadableError(f"cannot tell the unit of {said}: give it in euros")


def times(answer: str, start: int, end: int) -> list[re.Match]:
    """The times the answer gives at this place, each as a whole: a quote cannot cut off its am or pm."""
    here = lambda pattern: [m for m in pattern.finditer(answer) if m.start() < end and m.end() > start]      # noqa: E731
    return here(ISO) or here(CLOCK)


def moment(quote: str, answer: str, now: datetime, zone: str | None, span: tuple[int, int] | None = None) -> str:
    """The time a quote names in its answer, as ISO 8601. A weekday or a bare time is the next one after `now`."""
    said, answer = one_line(quote), normal(answer)
    places = [span] if span else spans(said, answer)
    if not places:
        raise UnreadableError(f"{said!r} is not in the answers")
    start, end = places[0]
    found = times(answer, start, end)
    if len(found) == 1 and found[0].re is ISO:
        try:
            return clock.parse(found[0][0].upper()).isoformat()
        except clock.ClockError as exc:
            raise UnreadableError(str(exc)) from exc
    days = {d.lower() for d in DAY.findall(answer[start:end])}
    if len(found) != 1 or len(days) > 1:
        raise UnreadableError(f"{said!r} must name one time and at most one day")
    t = found[0]
    if re.search(r"\d", answer[start:t.start()] + answer[t.end():end]):
        raise UnreadableError(f"give the date in {said!r} as ISO 8601 with a UTC offset")
    first, last = around(answer, min(start, t.start()), max(end, t.end()), SENTENCE)
    context = answer[first:last]
    if not days and DAY.search(context):
        raise UnreadableError(f"quote the day together with the time {said!r}")
    if OTHER_DAY.search(context):
        raise UnreadableError(f"give the date of {said!r} as ISO 8601 with a UTC offset")
    after = answer[t.end():]
    new_sentence = t[0].endswith(".") and re.match(r"\s+[A-Z]", after)
    if ZONE_NAME.search(context) or OTHER_TIME.search(context) or not (new_sentence or AFTER_TIME.match(after)):
        raise UnreadableError(f"cannot tell the zone or meaning of {said!r}: give the local time alone")

    hour, minute = int(t["h"] or t["h12"] or 12), int(t["m"] or 0)
    if half := (t["ap"] or t["ap12"] or "").lower().replace(".", ""):
        if hour > 12:
            raise UnreadableError(f"cannot read the time in {said!r}")
        hour = hour % 12 + (12 if half == "pm" else 0)
    elif not t["noon"] and ((hour <= 12 and LATE.search(context)) or (hour >= 12 and EARLY.search(context))):
        raise UnreadableError(f"give {said!r} with am or pm, or on the 24-hour clock")
    if not zone:
        raise UnreadableError(f"{said!r} needs the launch's time zone")
    try:
        tz = ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise UnreadableError(f"unknown time zone {zone!r}") from exc

    local = now.astimezone(tz)
    at = datetime.combine(local.date(), time(hour, minute), tz)
    day = next(iter(days), None)
    if day in RELATIVE:
        at += timedelta(days=RELATIVE[day])
    elif day:
        at += timedelta(days=(DAYS[day] - local.weekday()) % 7)
    # compared in UTC, so a change of the clocks cannot reorder them
    if at.astimezone(timezone.utc) <= now.astimezone(timezone.utc):
        if day in RELATIVE:
            raise UnreadableError(f"{said!r} has already passed")
        at += timedelta(days=7 if day else 1)
    return at.isoformat()


def money(value: float | int) -> str:
    return f"{value:,} EUR" if isinstance(value, int) else f"{value:,.2f} EUR"
