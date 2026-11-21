"""Higher-level signals on top of raw file changes: renames/format switches and stale files."""

from __future__ import annotations

from datetime import datetime
from fnmatch import fnmatchcase
from pathlib import PurePosixPath

from .config import StaleRule


def pair_renames(removed: list[str], added: list[str]) -> dict[str, str]:
    """Map new path -> old path when a file disappears and one with the same folder and stem appears
    in the same poll (e.g. open_shipments.csv -> open_shipments.json)."""
    by_stem: dict[str, list[str]] = {}
    for path in removed:
        p = PurePosixPath(path)
        by_stem.setdefault(str(p.with_suffix("")), []).append(path)
    pairs = {}
    for path in added:
        candidates = by_stem.get(str(PurePosixPath(path).with_suffix("")))
        if candidates:
            pairs[path] = candidates.pop(0)
    return pairs


def find_stale(rules: tuple[StaleRule, ...], last_change: dict[str, datetime], flagged: set[str],
               now: datetime) -> list[tuple[str, StaleRule, float]]:
    """Paths matching a rule that have not changed for longer than its threshold and are not yet flagged."""
    out = []
    for path, changed_at in last_change.items():
        if path in flagged:
            continue
        for rule in rules:
            if fnmatchcase(path, rule.pattern):
                age = (now - changed_at).total_seconds()
                if age > rule.seconds:
                    out.append((path, rule, age))
                break
    return out
