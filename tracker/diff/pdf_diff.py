"""PDF text diff. Image-only scans are flagged instead of producing an empty diff."""

from __future__ import annotations

import difflib
import io
import logging

from pypdf import PdfReader

logging.getLogger("pypdf").setLevel(logging.ERROR)

PREVIEW_CHARS = 4000


def extract(data: bytes) -> list[str]:
    reader = PdfReader(io.BytesIO(data))
    return [(page.extract_text() or "").strip() for page in reader.pages]


def _lines(pages: list[str]) -> list[str]:
    return [line.strip() for page in pages for line in page.splitlines() if line.strip()]


def diff(old: bytes, new: bytes, max_items: int) -> tuple[str, dict]:
    a, b = extract(old), extract(new)
    detail: dict = {"pages": [len(a), len(b)]}
    if not any(a) and not any(b):
        detail["scan"] = True
        return f"scanned PDF replaced (no text layer), pages {len(a)}→{len(b)}", detail
    if not any(b):
        detail["scan"] = True
        return "PDF replaced by a scan without text layer", detail
    la, lb = _lines(a), _lines(b)
    removed, added = [], []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, la, lb, autojunk=False).get_opcodes():
        if op in ("replace", "delete"):
            removed.extend(la[i1:i2])
        if op in ("replace", "insert"):
            added.extend(lb[j1:j2])
    detail["lines_removed"] = removed[:max_items]
    detail["lines_added"] = added[:max_items]
    detail["truncated"] = len(removed) > max_items or len(added) > max_items
    if not removed and not added:
        return "text unchanged (layout or metadata change only)", detail
    first = f": “{added[0][:80]}”" if added else ""
    return f"text changed, +{len(added)}/-{len(removed)} lines{first}", detail


def describe(data: bytes) -> tuple[str, dict]:
    pages = extract(data)
    text = "\n".join(pages)
    if not text.strip():
        return f"PDF, {len(pages)} pages, scan without text layer", {"pages": len(pages), "scan": True}
    return f"PDF, {len(pages)} pages", {"pages": len(pages), "text_preview": text[:PREVIEW_CHARS]}
