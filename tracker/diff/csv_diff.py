"""CSV parsing that copes with changing encodings, delimiters and headers."""

from __future__ import annotations

import csv
import io

from .records import Table, unique_headers

DELIMITERS = ",;\t|"
PREVIEW_ROWS = 50


def decode(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace"), "utf-8-sig"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16"), "utf-16"
    for encoding in ("utf-8", "cp1252"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            pass
    return data.decode("latin-1"), "latin-1"


def sniff_delimiter(text: str) -> str:
    sample = "\n".join(text.splitlines()[:30])
    try:
        return csv.Sniffer().sniff(sample, delimiters=DELIMITERS).delimiter
    except csv.Error:
        first = next((line for line in text.splitlines() if line.strip()), "")
        return max(DELIMITERS, key=first.count)


def parse(data: bytes) -> Table:
    text, encoding = decode(data)
    delimiter = sniff_delimiter(text)
    rows = [r for r in csv.reader(io.StringIO(text, newline=""), delimiter=delimiter) if any(c.strip() for c in r)]
    meta = {"encoding": encoding, "delimiter": delimiter, "line_ending": "CRLF" if "\r\n" in text else "LF"}
    if not rows:
        return Table([], [], meta)
    raw_header = rows[0]
    columns = unique_headers(raw_header)
    meta["raw_columns"] = raw_header
    records = []
    for row in rows[1:]:
        record = {col: (row[i] if i < len(row) else "") for i, col in enumerate(columns)}
        if len(row) > len(columns):
            record["_extra"] = delimiter.join(row[len(columns):])
        records.append(record)
    return Table(columns, records, meta)


def describe(table: Table) -> tuple[str, dict]:
    detail = {"rows": len(table.rows), "columns": table.columns, "preview": table.rows[:PREVIEW_ROWS],
              **{k: v for k, v in table.meta.items() if k != "raw_columns"}}
    return f"CSV, {len(table.rows)} rows, {len(table.columns)} columns", detail
