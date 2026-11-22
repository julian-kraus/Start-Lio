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
CLOCK = re.compile(r"(?<![\d:])(?:(?P<h>[01]?\d|2[0-3]):(?P<m>[0-5]\d)(?:\s*(?P<ap>[ap]m)\b)?(?![\d:])"
                   r"|(?P<h12>1[0-2]|0?[1-9])\s*(?P<ap12>[ap]m)\b|(?P<noon>\bnoon\b))", re.IGNORECASE)
DAYS = {word: day for day, words in enumerate(("mon monday", "tue tues tuesday", "wed wednesday",
                                               "thu thur thurs thursday", "fri friday", "sat saturday",
                                               "sun sunday")) for word in words.split()}
RELATIVE = {"today": 0, "tonight": 0, "tomorrow": 1}
DAY = re.compile(r"\b(" + "|".join(sorted([*DAYS, *RELATIVE], key=len, reverse=True)) + r")\b", re.IGNORECASE)
ISO = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d)?(?:Z|[+-]\d\d:\d\d)", re.IGNORECASE)
CLAUSE = re.compile(r"[.;!?\n](?:\s|$)|,\s")


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


def clauses(text: str, quote: str) -> list[str]:
    """The clauses of the text that contain the quote."""
    return [c for c in CLAUSE.split(text) if plain(quote) in plain(c)]


def moment(quote: str, answer: str, now: datetime, zone: str | None) -> str:
    """The time a quote names, as ISO 8601. A weekday or a bare time is the next one after `now` in `zone`."""
    said = one_line(quote)
    if iso := ISO.search(said):
        try:
            return clock.parse(iso[0].upper()).isoformat()
        except clock.ClockError as exc:
            raise UnreadableError(str(exc)) from exc
    times, days = list(CLOCK.finditer(said)), {d.lower() for d in DAY.findall(said)}
    if len(times) != 1 or len(days) > 1:
        raise UnreadableError(f"{said!r} must name one time and at most one day")
    if re.search(r"\d", said.replace(times[0][0], "", 1)):
        raise UnreadableError(f"give the date in {said!r} as ISO 8601 with a UTC offset")
    if not days and any(DAY.search(c) for c in clauses(answer, said)):
        raise UnreadableError(f"quote the day together with the time {said!r}")
    if not zone:
        raise UnreadableError(f"{said!r} needs the launch's time zone")
    try:
        tz = ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise UnreadableError(f"unknown time zone {zone!r}") from exc

    t = times[0]
    hour, minute = int(t["h"] or t["h12"] or 12), int(t["m"] or 0)
    if half := (t["ap"] or t["ap12"] or "").lower():
        if hour > 12:
            raise UnreadableError(f"cannot read the time in {said!r}")
        hour = hour % 12 + (12 if half == "pm" else 0)
    local = now.astimezone(tz)
    at = datetime.combine(local.date(), time(hour, minute), tz)
    day = next(iter(days), None)
    if day in RELATIVE:
        at += timedelta(days=RELATIVE[day])
    else:
        if day:
            at += timedelta(days=(DAYS[day] - local.weekday()) % 7)
        # compared in UTC, so a change of the clocks cannot reorder them
        if at.astimezone(timezone.utc) <= now.astimezone(timezone.utc):
            at += timedelta(days=7 if day else 1)
    return at.isoformat()


def money(value: float | int) -> str:
    return f"{value:,} EUR" if isinstance(value, int) else f"{value:,.2f} EUR"
