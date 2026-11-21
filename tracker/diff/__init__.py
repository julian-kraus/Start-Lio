"""Semantic diffs: what changed inside a file, dispatched on the detected content type."""

from __future__ import annotations

import difflib
import html
import io
import re
import logging
import zipfile
from pathlib import PurePosixPath

from . import csv_diff, json_diff, pdf_diff, xlsx_diff
from .records import diff_tables, summarize_table_diff

log = logging.getLogger(__name__)

PREVIEW_CHARS = 4000
IMAGE_MAGIC = {b"\x89PNG": "png", b"\xff\xd8\xff": "jpeg", b"GIF8": "gif"}


def detect_kind(path: str, data: bytes) -> str:
    """Content sniffing first (formats change), extension as a tie-breaker."""
    if data.startswith(b"%PDF"):
        return "pdf"
    for magic, name in IMAGE_MAGIC.items():
        if data.startswith(magic):
            return "image"
    if data.startswith(b"PK"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = set(zf.namelist())
            if "xl/workbook.xml" in names:
                return "xlsx"
            if "word/document.xml" in names:
                return "docx"
            return "zip"
        except zipfile.BadZipFile:
            return "binary"
    if b"\x00" in data[:4096] and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "binary"
    suffix = PurePosixPath(path).suffix.lower()
    stripped = data.lstrip(b"\xef\xbb\xbf \t\r\n")[:1]
    if suffix == ".json" or (stripped in (b"{", b"[") and suffix not in (".csv", ".tsv")):
        try:
            json_diff.load(data)
            return "json"
        except (ValueError, RecursionError):
            pass
    if suffix in (".csv", ".tsv"):
        return "csv"
    return "text"


def _docx_text(data: bytes) -> str:
    """Paragraph text of a .docx (stdlib only: unzip document.xml and strip the markup)."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        xml = zf.read("word/document.xml").decode("utf-8")
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab/>", "\t", xml)
    return html.unescape(re.sub(r"<[^>]+>", "", xml)).strip()


def _text(data: bytes, kind: str = "text") -> str:
    if kind == "docx":
        return _docx_text(data)
    return csv_diff.decode(data)[0]


def _as_table(kind: str, data: bytes):
    if kind == "csv":
        return csv_diff.parse(data)
    if kind == "json":
        return json_diff.as_table(json_diff.load(data))
    return None


def describe(path: str, data: bytes) -> tuple[str, dict]:
    """Summary and detail for a newly added file. Never raises."""
    kind = "binary"
    try:
        kind = detect_kind(path, data)
        if kind == "csv":
            summary, detail = csv_diff.describe(csv_diff.parse(data))
        elif kind == "json":
            summary, detail = json_diff.describe(json_diff.load(data))
        elif kind == "xlsx":
            summary, detail = xlsx_diff.describe(data)
        elif kind == "pdf":
            summary, detail = pdf_diff.describe(data)
        elif kind in ("text", "docx"):
            text = _text(data, kind)
            summary, detail = f"{kind}, {len(text.splitlines())} lines", {"text_preview": text[:PREVIEW_CHARS]}
        else:
            summary, detail = f"{kind}, {len(data):,} bytes", {}
    except Exception as exc:  # a parser failing must never stop the tracker
        log.warning("describe failed for %s: %s", path, exc)
        summary, detail = f"{kind}, {len(data):,} bytes (could not parse: {exc})", {"parse_error": str(exc)}
    return summary, {"kind": kind, "bytes": len(data), **detail}


def compute(old_path: str, old: bytes, new_path: str, new: bytes, max_items: int = 500) -> tuple[str, dict]:
    """Summary and detail for a file whose content changed (or that moved to a new path/format). Never raises."""
    base: dict = {"bytes": [len(old), len(new)]}
    try:
        old_kind, new_kind = detect_kind(old_path, old), detect_kind(new_path, new)
        base["kind"] = new_kind
        if old_kind != new_kind:
            base["kind_changed"] = [old_kind, new_kind]
        if old_kind == new_kind == "csv" or {old_kind, new_kind} <= {"csv", "json"} and old_kind != new_kind:
            ta, tb = _as_table(old_kind, old), _as_table(new_kind, new)
            if ta is not None and tb is not None:
                detail = diff_tables(ta, tb, max_items)
                return summarize_table_diff(detail), {**base, **detail}
        if old_kind == new_kind == "json":
            summary, detail = json_diff.diff(old, new, max_items)
        elif old_kind == new_kind == "xlsx":
            summary, detail = xlsx_diff.diff(old, new, max_items)
        elif old_kind == new_kind == "pdf":
            summary, detail = pdf_diff.diff(old, new, max_items)
        elif old_kind == new_kind and old_kind in ("text", "docx"):
            la, lb = _text(old, old_kind).splitlines(), _text(new, new_kind).splitlines()
            lines = list(difflib.unified_diff(la, lb, lineterm="", n=1))[2:]
            detail = {"unified_diff": lines[:max_items], "truncated": len(lines) > max_items}
            added = sum(1 for l in lines if l.startswith("+"))
            removed = sum(1 for l in lines if l.startswith("-"))
            summary = f"text changed, +{added}/-{removed} lines"
        else:
            summary = f"{new_kind} content replaced, {len(old):,}→{len(new):,} bytes"
            if old_kind != new_kind:
                summary = f"format changed {old_kind}→{new_kind}, {len(old):,}→{len(new):,} bytes"
            detail = {}
    except Exception as exc:
        log.warning("diff failed for %s: %s", new_path, exc)
        summary, detail = f"content changed (diff failed: {exc})", {"diff_error": str(exc)}
    return summary, {**base, **detail}
