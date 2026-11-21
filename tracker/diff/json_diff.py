"""Structural JSON diff. Lists of objects are diffed row by row like tables."""

from __future__ import annotations

import json

from .records import Table, diff_tables, summarize_table_diff, unique_headers


def load(data: bytes):
    return json.loads(data.decode("utf-8-sig"))


def _scalar(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _is_records(value) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(v, dict) for v in value)


def records_to_table(items: list[dict]) -> Table:
    raw_cols = list(dict.fromkeys(k for item in items for k in item))
    cols = unique_headers(raw_cols)
    rows = [{c: _scalar(item.get(raw)) for raw, c in zip(raw_cols, cols)} for item in items]
    return Table(cols, rows, {})


def as_table(obj) -> Table | None:
    """A JSON document that is (or wraps exactly one) list of objects, as a table."""
    if _is_records(obj):
        return records_to_table(obj)
    if isinstance(obj, dict):
        lists = [v for v in obj.values() if _is_records(v)]
        if len(lists) == 1:
            return records_to_table(lists[0])
    return None


MAX_DEPTH = 64


def _tree(a, b, path: str, out: dict, max_items: int, depth: int = 0) -> None:
    if len(out["changes"]) + len(out["added"]) + len(out["removed"]) >= max_items:
        out["truncated"] = True
        return
    if depth >= MAX_DEPTH:  # pathological nesting: compare the subtree as a whole
        if a != b:
            out["changes"].append({"path": path, "old": "(deeply nested value)", "new": "(changed)"})
        return
    if isinstance(a, dict) and isinstance(b, dict):
        for k in b:
            sub = f"{path}.{k}" if path else str(k)
            if k not in a:
                out["added"].append({"path": sub, "value": b[k]})
            else:
                _tree(a[k], b[k], sub, out, max_items, depth + 1)
        for k in a:
            if k not in b:
                out["removed"].append({"path": f"{path}.{k}" if path else str(k), "value": a[k]})
    elif _is_records(a) and _is_records(b):
        table_diff = diff_tables(records_to_table(a), records_to_table(b), max_items)
        if table_diff["rows_added"] or table_diff["rows_removed"] or table_diff["rows_changed"] or "schema" in table_diff:
            out["tables"][path or "$"] = table_diff
    elif a != b:
        out["changes"].append({"path": path or "$", "old": a, "new": b})


def diff(old: bytes, new: bytes, max_items: int) -> tuple[str, dict]:
    out = {"changes": [], "added": [], "removed": [], "tables": {}, "truncated": False}
    _tree(load(old), load(new), "", out, max_items)
    parts = [f"{c['path']}: {_scalar(c['old'])[:40]}→{_scalar(c['new'])[:40]}" for c in out["changes"][:3]]
    if len(out["changes"]) > 3:
        parts.append(f"+{len(out['changes']) - 3} more value changes")
    if out["added"]:
        parts.append(f"{len(out['added'])} keys added")
    if out["removed"]:
        parts.append(f"{len(out['removed'])} keys removed")
    for path, table_diff in out["tables"].items():
        parts.append(f"{path}: {summarize_table_diff(table_diff)}")
    return ", ".join(parts) or "no content change", out


def describe(obj) -> tuple[str, dict]:
    if isinstance(obj, list):
        return f"JSON list, {len(obj)} items", {"type": "list", "items": len(obj)}
    if isinstance(obj, dict):
        return f"JSON object, keys: {', '.join(list(obj)[:10])}", {"type": "object", "keys": list(obj)}
    return "JSON scalar", {"type": type(obj).__name__}
