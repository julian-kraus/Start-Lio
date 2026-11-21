"""Cell-level XLSX diff with row and column labels for context."""

from __future__ import annotations

import io
from datetime import date, datetime, time

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from .records import normalize_value

Sheet = dict[tuple[int, int], object]


def _clean(value):
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


def load(data: bytes) -> dict[str, Sheet]:
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        sheets = {}
        for ws in wb.worksheets:
            cells: Sheet = {}
            for r, row in enumerate(ws.iter_rows(values_only=True), start=1):
                for c, value in enumerate(row, start=1):
                    value = _clean(value)
                    if value is not None:
                        cells[(r, c)] = value
            sheets[ws.title] = cells
        return sheets
    finally:
        wb.close()


def _row_label(sheet: Sheet, row: int) -> str | None:
    for c in range(1, 4):
        if (row, c) in sheet:
            return str(sheet[(row, c)])
    return None


def diff(old: bytes, new: bytes, max_items: int) -> tuple[str, dict]:
    a, b = load(old), load(new)
    detail = {
        "sheets_added": [s for s in b if s not in a],
        "sheets_removed": [s for s in a if s not in b],
        "cells": {},
        "format_only_changes": 0,
        "truncated": False,
    }
    budget = max_items
    for name in (s for s in b if s in a):
        old_s, new_s = a[name], b[name]
        changes = []
        for pos in sorted(set(old_s) | set(new_s)):
            ov, nv = old_s.get(pos), new_s.get(pos)
            if ov == nv:
                continue
            if normalize_value(ov) == normalize_value(nv):
                detail["format_only_changes"] += 1
                continue
            if budget <= 0:
                detail["truncated"] = True
                break
            budget -= 1
            r, c = pos
            changes.append({
                "cell": f"{get_column_letter(c)}{r}",
                "old": ov,
                "new": nv,
                "row_label": _row_label(new_s, r) or _row_label(old_s, r),
                "column_label": new_s.get((1, c), old_s.get((1, c))),
            })
        if changes:
            detail["cells"][name] = changes
    parts = []
    if detail["sheets_added"]:
        parts.append(f"sheets added: {', '.join(detail['sheets_added'])}")
    if detail["sheets_removed"]:
        parts.append(f"sheets removed: {', '.join(detail['sheets_removed'])}")
    for name, changes in detail["cells"].items():
        shown = "; ".join(
            f"{ch['cell']}{' (' + str(ch['row_label']) + ' / ' + str(ch['column_label']) + ')' if ch['row_label'] else ''}"
            f" {ch['old']}→{ch['new']}"
            for ch in changes[:3]
        )
        parts.append(f"{name}: {len(changes)} cells changed ({shown})")
    if detail["format_only_changes"]:
        parts.append(f"{detail['format_only_changes']} format-only cell changes")
    return ", ".join(parts) or "no content change", detail


PREVIEW_ROWS = 40


def _preview(cells: Sheet) -> list[list]:
    """Top rows of a sheet as a grid (empty leading/trailing columns dropped)."""
    if not cells:
        return []
    rows = sorted({r for r, _ in cells})[:PREVIEW_ROWS]
    cols = sorted({c for r, c in cells if r in set(rows)})
    return [[cells.get((r, c)) for c in range(cols[0], cols[-1] + 1)] for r in rows]


def describe(data: bytes) -> tuple[str, dict]:
    sheets = load(data)
    dims = {name: {"cells": len(cells), "rows": max((r for r, _ in cells), default=0)} for name, cells in sheets.items()}
    preview = {name: _preview(cells) for name, cells in sheets.items()}
    return f"XLSX, sheets: {', '.join(sheets)}", {"sheets": dims, "sheet_preview": preview}
