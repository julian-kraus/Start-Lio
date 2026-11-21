"""Tabular ingest driven by schema maps (format in challenge/codex-handover.md).

The header row is found by signature, not by position: every one of the first rows is hashed and compared with the
known maps. A new or renamed column therefore means "new signature", which is a visible issue, not a crash or a
silently wrong parse. Nothing here knows any file, column or value of the Soony data.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass
from datetime import date, datetime, time

from openpyxl import load_workbook

from tracker.diff import csv_diff, json_diff

HEADER_SCAN_ROWS = 15
TABULAR_CONTAINERS = ("csv", "xlsx", "json")


@dataclass
class RawTable:
    sheet: str | None
    rows: list[list[str]]          # cell texts, as written
    row_numbers: list[int]         # 1-based line or row number in the source, for locators


def signature(headers: list, sheet: str | None = None) -> str:
    names = sorted(str(h).strip().lower() for h in headers if h is not None and str(h).strip())
    text = "|".join(names)
    if sheet is not None:
        text += "|sheet:" + sheet.strip().lower()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def read_tables(container: str, data: bytes) -> list[RawTable]:
    if container == "csv":
        text, _ = csv_diff.decode(data)
        delimiter = csv_diff.sniff_delimiter(text)
        rows, numbers = [], []
        for i, row in enumerate(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter), start=1):
            if any(c.strip() for c in row):
                rows.append([c.strip() for c in row])
                numbers.append(i)
        return [RawTable(None, rows, numbers)]
    if container == "xlsx":
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        try:
            tables = []
            for ws in wb.worksheets:
                rows, numbers = [], []
                for i, row in enumerate(ws.iter_rows(values_only=True), start=1):
                    cells = [_cell_text(v) for v in row]
                    while cells and not cells[-1]:
                        cells.pop()
                    if cells:
                        rows.append(cells)
                        numbers.append(i)
                tables.append(RawTable(ws.title, rows, numbers))
            return tables
        finally:
            wb.close()
    if container == "json":
        obj = json_diff.load(data)
        items = obj if isinstance(obj, list) else next(
            (v for v in obj.values() if isinstance(v, list) and v and all(isinstance(x, dict) for x in v)), [])
        if not items or not all(isinstance(x, dict) for x in items):
            return []
        cols = list(dict.fromkeys(k for item in items for k in item))
        rows = [cols] + [[_cell_text(json_diff._scalar(item.get(c))) for c in cols] for item in items]
        return [RawTable(None, rows, list(range(0, len(rows))))]
    return []


def find_header(table: RawTable, maps: dict[str, dict], container: str) -> tuple[int, dict] | None:
    sheet = table.sheet if container == "xlsx" else None
    for i, row in enumerate(table.rows[:HEADER_SCAN_ROWS]):
        mapping = maps.get(signature(row, sheet))
        if mapping:
            return i, mapping
    return None


def guess_header(table: RawTable) -> list[str]:
    """Best guess for an unmapped table, only used to describe the problem in an issue."""
    for row in table.rows[:HEADER_SCAN_ROWS]:
        texts = [c for c in row if c and not _looks_numeric(c)]
        if len(texts) >= 2 and len(texts) >= len([c for c in row if c]) * 0.6:
            return row
    return table.rows[0] if table.rows else []


def _looks_numeric(text: str) -> bool:
    return bool(re.fullmatch(r"[+-]?[\d.,\s ]+", text))


def parse_value(text: str, spec: dict):
    """Returns (value, error). Empty cells are None without error."""
    if text is None or text == "":
        return None, None
    enum = spec.get("enum")
    if enum:
        if text in enum:
            return enum[text], None
        folded = {str(k).casefold(): v for k, v in enum.items()}
        if text.casefold() in folded:
            return folded[text.casefold()], None
    kind = spec.get("type", "text")
    try:
        if kind in ("int", "decimal"):
            cleaned = re.sub(r"[\s  ]", "", text)
            if spec.get("thousands_sep"):
                cleaned = cleaned.replace(spec["thousands_sep"], "")
            if spec.get("decimal_sep") and spec["decimal_sep"] != ".":
                cleaned = cleaned.replace(spec["decimal_sep"], ".")
            number = float(cleaned)
            if kind == "int":
                if not number.is_integer():
                    return None, f"not an integer: {text!r}"
                number = int(number)
            if spec.get("id_template"):
                return spec["id_template"].format(int(number)), None
            return number, None
        if kind in ("date", "datetime"):
            fmt = spec.get("date_format")
            parsed = datetime.strptime(text, fmt) if fmt else datetime.fromisoformat(text)
            return (parsed.date().isoformat() if kind == "date" else parsed.isoformat()), None
        if kind == "bool":
            folded = text.casefold()
            if folded in ("y", "yes", "true", "1", "x", "ja", "j"):
                return True, None
            if folded in ("n", "no", "false", "0", "nein"):
                return False, None
            return None, f"not a boolean: {text!r}"
        if kind == "list":
            sep = spec.get("list_sep", "|")
            return [p.strip() for p in text.split(sep) if p.strip()], None
        if spec.get("id_template") and text.isdigit():
            return spec["id_template"].format(int(text)), None
        return text, None
    except (ValueError, KeyError, IndexError) as exc:
        return None, f"{kind} parse failed for {text!r}: {exc}"


def table_to_facts(table: RawTable, header_index: int, mapping: dict, *, path: str, sha256: str,
                   extractor: str = "kb:tabular") -> tuple[list[dict], dict]:
    header = table.rows[header_index]
    columns = mapping.get("columns", {})
    subject_fields = list(mapping.get("subject") or [])
    fact_type = mapping["fact_type"]
    as_of_spec = mapping.get("as_of") or {}
    col_specs = []
    for idx, name in enumerate(header):
        spec = columns.get(name)
        if spec is None:
            spec = next((s for k, s in columns.items() if k.strip().lower() == name.strip().lower()), None)
        if spec and spec.get("field"):
            col_specs.append((idx, name, spec))
    stats = {"rows": 0, "skipped_rows": 0, "parse_errors": 0, "error_samples": []}
    facts = []
    for row, line in zip(table.rows[header_index + 1:], table.row_numbers[header_index + 1:]):
        parsed, raw, errors = {}, {}, {}
        for idx, name, spec in col_specs:
            text = row[idx] if idx < len(row) else ""
            value, error = parse_value(text, spec)
            parsed[spec["field"]] = value
            raw[spec["field"]] = text
            if error:
                errors[spec["field"]] = error
        subject = {f: parsed.get(f) for f in subject_fields if parsed.get(f) not in (None, "")}
        if subject_fields and len(subject) < len(subject_fields):
            stats["skipped_rows"] += 1
            continue
        if not any(v not in (None, "") for v in parsed.values()):
            continue
        stats["rows"] += 1
        if errors:
            stats["parse_errors"] += len(errors)
            if len(stats["error_samples"]) < 10:
                stats["error_samples"].append({"line": line, **errors})
        as_of = None
        if as_of_spec.get("field"):
            as_of = parsed.get(as_of_spec["field"])
        elif as_of_spec.get("value"):
            as_of = as_of_spec["value"]
        locator = {"row": line}
        if table.sheet is not None:
            locator["sheet"] = table.sheet
        subject_raw = {f: raw.get(f) for f in subject_fields}
        for _, name, spec in col_specs:
            field_name = spec["field"]
            if field_name in subject_fields:
                continue
            value = parsed.get(field_name)
            if value is None and not raw.get(field_name):
                continue
            facts.append({
                "fact_type": fact_type, "subject": subject, "subject_raw": subject_raw,
                "attribute": field_name, "value": value, "unit": spec.get("unit"), "as_of": as_of,
                "locator": {**locator, "column": name}, "evidence": raw.get(field_name),
                "extractor": extractor, "confidence": 1.0 if field_name not in errors else 0.3,
                "checks": {"parse_error": errors[field_name]} if field_name in errors else None,
                "source_path": path, "source_sha256": sha256,
            })
    return facts, stats
