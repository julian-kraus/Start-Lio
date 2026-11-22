"""Which takes of added supply can count, part by part.

Earliest first, so one more action can never push a pool's units to a later hour. A minimum breaks that, as an
earlier take can clip a later one to nothing. Leaving a take out is then tried too, and the caller picks among the
ways that remain by what the plan can pack with them.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime

from .coverage_windows import Window, hours


@dataclass
class Way:
    """One way to take the supplies of a part."""

    takes: list                 # (supply, units, units that count)
    taken: Counter              # units out of each pool
    landed: list                # (arrives_at, units) as taken, in time order
    packed: int                 # units of the part that can still be used

    def counted(self) -> list[tuple[datetime, int]]:
        return [(s.at, counts) for s, _, counts in self.takes if counts]


def packed_by_deadline(on_hand, arrivals: list[tuple], burn: float, windows: list[Window]) -> int:
    """Units of one part that can still be used. Pack time lost while the part was out does not come back."""
    total = on_hand + sum(qty for _, qty in arrivals)
    if not burn or not windows:
        return int(total)
    idle = _idle(on_hand, arrivals, burn, windows)
    if not idle:
        return int(total)
    return int(min(total, burn * hours(windows) - idle) + 1e-9)     # 1e-9: float noise


def _idle(on_hand, arrivals: list[tuple], burn: float, windows: list[Window]) -> float:
    """What could have been packed while the part was out, up to its last arrival."""
    have, idle, by_time = on_hand, 0, defaultdict(int)
    for at, qty in arrivals:
        by_time[at] += qty
    for at in sorted(by_time):
        idle = max(idle, burn * hours(windows, until=at) - have)
        have += by_time[at]
    return idle


def _as_early(these: list[tuple], those: list[tuple]) -> bool:
    """Whether these lots have, at every hour, at least as many units in as those. Both are in time order."""
    have = needed = n = 0
    for at, qty in those:
        needed += qty
        while n < len(these) and these[n][0] <= at:
            have += these[n][1]
            n += 1
        if have < needed:
            return False
    return True


def _undominated(ways: list[Way], lots) -> list[Way]:
    """Units that are in no later are never worse for any line, so only ways that win at some hour stay."""
    kept: list[tuple[Way, list]] = []
    for way in ways:
        mine = lots(way)
        if not any(_as_early(theirs, mine) for _, theirs in kept):
            kept = [(w, theirs) for w, theirs in kept if not _as_early(mine, theirs)] + [(way, mine)]
    return [way for way, _ in kept]


def ways_to_take(supplies: list, on_hand, arrivals: list[tuple], burn: float, windows: list[Window]) -> list[Way]:
    """The ways worth comparing for one part, with supplies in time order. One, unless a minimum makes them compete."""
    ways = [Way([], Counter(), [], packed_by_deadline(on_hand, arrivals, burn, windows))]
    for n, s in enumerate(supplies):
        rivals = [r for r in supplies[n + 1:] if r.pool == s.pool]
        grown = defaultdict(list)
        for way in ways:
            units = min(s.quantity, max(0, s.limit - way.taken[s.pool]))
            options = [units] if units and units >= s.minimum else [0]
            after = way.taken[s.pool] + units + sum(r.quantity for r in rivals)
            if options[0] and any(r.minimum and after > r.limit for r in rivals):
                options.append(0)                       # taking it may cost a later take its minimum
            for got in options:
                landed, taken, packed, counts = way.landed, way.taken, way.packed, 0
                if got:
                    landed, taken = [*landed, (s.at, got)], taken + Counter({s.pool: got})
                    packed = packed_by_deadline(on_hand, [*arrivals, *landed], burn, windows)
                    counts = min(got, max(0, packed - way.packed))
                grown[frozenset(taken.items())].append(Way([*way.takes, (s, got, counts)], taken, landed, packed))
        # with the same left in every pool, no later take can tell two ways apart except by when units came in
        ways = [w for same in grown.values() for w in _undominated(same, lambda way: way.landed)]
    return _undominated(sorted(ways, key=lambda w: -sum(w.taken.values())), Way.counted)
