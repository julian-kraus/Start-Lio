"""One part split over the editions that share it: in policy order, and never beyond what each line can pack.

A part is a list of lots in arrival order, (arrives_at, units), usable stock first with None. Units that arrive
after a line ran out only help for that line's remaining hours, so each edition has a limit of its own.
"""

from __future__ import annotations

import math
from datetime import datetime

from .coverage_windows import Window, hours

EPS = 1e-6                  # float noise: 1000 x 0.1 is still 1000 copies
Lots = list[tuple[datetime | None, float]]


def whole(value) -> int:
    return math.floor(value + EPS)


def reach(lots: Lots, rate: float, windows: list[Window]) -> float:
    """The units one line can still use, taking `rate` of them an hour."""
    most, before = sum(n for _, n in lots), 0.0
    if not rate or not windows:
        return most
    for at, n in lots:
        if at is not None and before + EPS < rate * hours(windows, until=at):
            most = min(most, before + rate * hours(windows, since=at))          # it stood still until this came
        before += n
    return round(most, 6)


def take(lots: Lots, units: float) -> Lots:
    """The lots left after a reservation, which takes the earliest first."""
    left = []
    for at, n in lots:
        used = min(n, units)
        units -= used
        left.append((at, n - used))
    return left


def share(lots: Lots, wants: dict[str, float], rates: dict[str, float], order: list[str] | None,
          windows: list[Window]) -> dict[str, float]:
    """Split one part: in policy order, or in proportion to need when no policy decides."""
    total, have = sum(wants.values()), sum(n for _, n in lots)
    units = all(float(w).is_integer() for w in wants.values())
    out, left = {}, lots
    for product in [p for p in order if p in wants] if order is not None else wants:
        want, pool = wants[product], left
        if order is None and total:
            pool = [(at, n * want / total) for at, n in lots]
            if total > have + EPS:
                want = have * want / total
        got = min(want, reach(pool, rates[product], windows))
        out[product] = whole(got) if units else round(got, 6)
        if order is not None:
            left = take(left, out[product])
    return out


def packable(eds: list, boms: dict, lots: dict[str, Lots], order: list[str] | None, allocated: dict,
             windows: list[Window]) -> dict[str, tuple[int, list[str]]]:
    """Copies per edition and the parts that limit it."""
    out, left = {}, dict(lots)
    by_id = {e.product_id: e for e in eds}
    for product in order or by_id:
        edition, bom = by_id[product], boms[product]
        pool = {m: reach(left[m], (edition.rate or 0) * qty, windows) if order else allocated[m][product]
                for m, (qty, _) in bom.items()}
        limits = {m: whole(pool[m] / qty) for m, (qty, _) in bom.items()}
        copies = min([edition.open, *limits.values()]) if bom else 0
        out[product] = copies, sorted(m for m, n in limits.items() if n == copies and n < edition.open)
        if order:
            for m, (qty, _) in bom.items():
                left[m] = take(left[m], copies * qty)
    return out
