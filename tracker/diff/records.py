"""Row-level diff for tabular data (CSV, JSON lists of objects), robust to format drift.

Column names are normalised, the key column is detected from the data, and values that only
changed format (e.g. '2026-11-19' vs '19.11.2026', '1.5' vs '1,5') are counted separately
instead of being reported as content changes.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import combinations

KEY_HINT = re.compile(r"(^id$|_id$|^id_|number|_no$|^no$|_nr$|^nr$|ebeln|ebelp|batch|reference|^ref|code$)")
NUMBER = re.compile(r"[+-]?\d+(?:[.,]\d+)?")
DATE_FORMATS = ("%Y-%m-%d", "%d.%m.%Y", "%Y/%m/%d", "%d-%m-%Y")
KEYLESS_MAX_CELLS = 4_000_000  # old rows x new rows above which the keyless diff skips row pairing
RENAME_MIN_OVERLAP = 0.5
SUMMARY_SNIPPETS = 3


@dataclass
class Table:
    columns: list[str]
    rows: list[dict[str, str]]
    meta: dict = field(default_factory=dict)


def normalize_header(name: str) -> str:
    name = name.replace("﻿", "").strip().lower()
    return re.sub(r"[\s\-./]+", "_", name).strip("_") or "column"


def unique_headers(raw: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for name in raw:
        base = normalize_header(name)
        seen[base] = seen.get(base, 0) + 1
        out.append(base if seen[base] == 1 else f"{base}_{seen[base]}")
    return out


def _number(s: str) -> str | None:
    """Canonical number, only where the reading is unambiguous. '1,500' / '1.500' could be a thousands
    separator or a decimal, so they are left as-is (reported as a change rather than risk hiding one)."""
    if not NUMBER.fullmatch(s):
        return None
    sep = next((c for c in ",." if c in s), None)
    if sep and len(s.split(sep, 1)[1]) == 3:
        return None
    return repr(float(s.replace(",", ".")))


def _slash_date(s: str) -> str | None:
    """a/b/yyyy is only normalised when the day is recognisable (a part above 12)."""
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if not m:
        return None
    a, b, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if a > 12 >= b:
        day, month = a, b
    elif b > 12 >= a:
        day, month = b, a
    elif a == b:
        day = month = a
    else:
        return None
    try:
        return datetime(year, month, day).date().isoformat()
    except ValueError:
        return None


def normalize_value(value) -> str:
    """Canonical form used only to decide whether two differing strings mean the same thing.
    Ambiguous readings fall through unchanged, so a format change is never allowed to hide a real one."""
    s = "" if value is None else str(value).strip()
    if not s:
        return ""
    number = _number(s)
    if number is not None:
        return number
    if NUMBER.fullmatch(s):
        return s
    slash = _slash_date(s)
    if slash is not None:
        return slash
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    if "T" in s or " " in s:
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo:
                dt = dt.astimezone(timezone.utc)
            return dt.isoformat()
        except ValueError:
            pass
    return s


def _key_set(rows: list[dict[str, str]], cols: tuple[str, ...]) -> set | None:
    """The set of key values, or None if the columns are not a valid (non-empty, unique) key."""
    seen = set()
    for row in rows:
        key = tuple((row.get(c) or "").strip() for c in cols)
        if not any(key) or key in seen:
            return None
        seen.add(key)
    return seen


def pick_key(columns: list[str], *tables: list[dict[str, str]]) -> tuple[str, ...] | None:
    """Pick the column (or pair) that is a valid key in every table and whose values overlap most
    between the tables, so a changed quantity is never mistaken for an identifier."""
    if not columns or not all(tables):
        return None
    hinted = [c for c in columns if KEY_HINT.search(c)]
    pair_pool = list(dict.fromkeys(hinted + columns[:6]))[:8]
    candidates = [(c,) for c in columns] + list(combinations(pair_pool, 2))
    best, best_score = None, None
    for rank, cols in enumerate(candidates):
        sets = [_key_set(t, cols) for t in tables]
        if any(s is None for s in sets):
            continue
        overlap = len(set.intersection(*sets)) / max(len(s) for s in sets)
        score = (round(overlap, 3), sum(bool(KEY_HINT.search(c)) for c in cols) / len(cols), -len(cols), -rank)
        if best_score is None or score > best_score:
            best, best_score = cols, score
    return best


def _column_values(rows: list[dict[str, str]], col: str) -> set[str]:
    return {normalize_value(r.get(col)) for r in rows} - {""}


def _detect_renames(old: "Table", new: "Table") -> dict[str, str]:
    """A dropped column and a new one count as a rename only if their values largely overlap
    (so 'price' dropped + 'supplier' added stays a drop and an add)."""
    removed = [c for c in old.columns if c not in new.columns]
    added = [c for c in new.columns if c not in old.columns]
    if not removed or not added:
        return {}
    old_vals = {c: _column_values(old.rows, c) for c in removed}
    new_vals = {c: _column_values(new.rows, c) for c in added}
    scored = []
    for o in removed:
        for n in added:
            a, b = old_vals[o], new_vals[n]
            if a and b:
                overlap = len(a & b) / min(len(a), len(b))
                if overlap >= RENAME_MIN_OVERLAP:
                    same_position = old.columns.index(o) == new.columns.index(n)
                    scored.append((overlap, same_position, o, n))
    renames: dict[str, str] = {}
    for _, _, o, n in sorted(scored, reverse=True):
        if o not in renames and n not in renames.values():
            renames[o] = n
    return renames


def _key_label(row: dict[str, str], key: tuple[str, ...]) -> str:
    return " | ".join((row.get(c) or "").strip() for c in key)


def _compare_rows(old: dict, new: dict, cols: list[str], format_only: dict[str, int]) -> dict:
    changes = {}
    for col in cols:
        a, b = (old.get(col) or "").strip(), (new.get(col) or "").strip()
        if a == b:
            continue
        if normalize_value(a) == normalize_value(b):
            format_only[col] = format_only.get(col, 0) + 1
        else:
            changes[col] = [a, b]
    return changes


def diff_tables(old: Table, new: Table, max_items: int = 500) -> dict:
    renames = _detect_renames(old, new)
    old_rows = [{renames.get(k, k): v for k, v in r.items()} for r in old.rows]
    old_cols = [renames.get(c, c) for c in old.columns]
    common = [c for c in new.columns if c in old_cols]

    detail: dict = {
        "rows_total": {"old": len(old.rows), "new": len(new.rows)},
        "rows_added": [],
        "rows_removed": [],
        "rows_changed": [],
        "truncated": False,
    }
    fmt = {k: [old.meta.get(k), new.meta.get(k)] for k in new.meta
           if k != "raw_columns" and old.meta.get(k) != new.meta.get(k)}
    if fmt:
        detail["format"] = fmt
    schema = {
        "columns_added": [c for c in new.columns if c not in old_cols],
        "columns_removed": [c for c in old.columns if c not in renames and c not in new.columns],
        "columns_renamed": renames,
    }
    if any(schema.values()):
        detail["schema"] = schema

    format_only: dict[str, int] = {}
    key = pick_key(common, old_rows, new.rows)
    detail["key"] = list(key) if key else None
    budget = [max_items]

    def push(bucket: str, item: dict) -> None:
        if budget[0] <= 0:
            detail["truncated"] = True
            return
        budget[0] -= 1
        detail[bucket].append(item)

    if key:
        old_idx = {_key_label(r, key): r for r in old_rows}
        new_idx = {_key_label(r, key): r for r in new.rows}
        for k, row in new_idx.items():
            if k not in old_idx:
                push("rows_added", {"key": k, "row": row})
            else:
                changes = _compare_rows(old_idx[k], row, common, format_only)
                if changes:
                    push("rows_changed", {"key": k, "changes": changes})
        for k, row in old_idx.items():
            if k not in new_idx:
                push("rows_removed", {"key": k, "row": row})
    elif len(old_rows) * len(new.rows) > KEYLESS_MAX_CELLS:
        # Too large to pair rows up (SequenceMatcher is quadratic): report a multiset difference instead.
        from collections import Counter

        def sig(row):
            return tuple(normalize_value(row.get(c)) for c in common)

        old_count, new_count = Counter(map(sig, old_rows)), Counter(map(sig, new.rows))
        gone, came = old_count - new_count, new_count - old_count
        detail["pairing"] = "skipped (large table without a key)"
        for j, row in enumerate(new.rows):
            if came[sig(row)] > 0:
                came[sig(row)] -= 1
                push("rows_added", {"key": f"row {j + 2}", "row": row})
        for i, row in enumerate(old_rows):
            if gone[sig(row)] > 0:
                gone[sig(row)] -= 1
                push("rows_removed", {"key": f"row {i + 2}", "row": row})
    else:
        def sig(row):
            return tuple(normalize_value(row.get(c)) for c in common)

        matcher = difflib.SequenceMatcher(None, [sig(r) for r in old_rows], [sig(r) for r in new.rows], autojunk=False)
        for op, i1, i2, j1, j2 in matcher.get_opcodes():
            if op == "equal":
                for i, j in zip(range(i1, i2), range(j1, j2)):
                    _compare_rows(old_rows[i], new.rows[j], common, format_only)
                continue
            paired = min(i2 - i1, j2 - j1) if op == "replace" else 0
            for n in range(paired):
                changes = _compare_rows(old_rows[i1 + n], new.rows[j1 + n], common, format_only)
                if changes:
                    push("rows_changed", {"key": f"row {j1 + n + 2}", "changes": changes})
            for i in range(i1 + paired, i2):
                push("rows_removed", {"key": f"row {i + 2}", "row": old_rows[i]})
            for j in range(j1 + paired, j2):
                push("rows_added", {"key": f"row {j + 2}", "row": new.rows[j]})
    if format_only:
        detail["format_only_changes"] = format_only
    if key and schema["columns_added"]:
        # Values of new columns are content too (e.g. a new day_one_qty column).
        detail["added_column_values"] = {
            col: {_key_label(r, key): r.get(col, "") for r in new.rows[:max_items]} for col in schema["columns_added"]
        }
    return detail


def summarize_table_diff(detail: dict) -> str:
    parts = []
    for bucket, label in (("rows_changed", "changed"), ("rows_added", "added"), ("rows_removed", "removed")):
        n = len(detail.get(bucket, []))
        if n:
            parts.append(f"{n}{'+' if detail.get('truncated') else ''} rows {label}")
    schema = detail.get("schema", {})
    if schema.get("columns_added"):
        parts.append(f"columns added: {', '.join(schema['columns_added'])}")
    if schema.get("columns_removed"):
        parts.append(f"columns removed: {', '.join(schema['columns_removed'])}")
    if schema.get("columns_renamed"):
        parts.append("columns renamed: " + ", ".join(f"{a}→{b}" for a, b in schema["columns_renamed"].items()))
    if detail.get("format"):
        parts.append("format: " + ", ".join(f"{k} {a!r}→{b!r}" for k, (a, b) in detail["format"].items()))
    if detail.get("format_only_changes"):
        parts.append(f"format-only value changes in {', '.join(detail['format_only_changes'])}")
    snippets = []
    for item in detail.get("rows_changed", [])[:SUMMARY_SNIPPETS]:
        fields = "; ".join(f"{c} {a or '∅'}→{b or '∅'}" for c, (a, b) in list(item["changes"].items())[:3])
        snippets.append(f"{item['key']}: {fields}")
    for item in detail.get("rows_added", [])[: max(0, SUMMARY_SNIPPETS - len(snippets))]:
        snippets.append(f"+{item['key']}")
    text = ", ".join(parts) or "no content change"
    if snippets:
        text += " (" + "; ".join(snippets) + ")"
    return text
