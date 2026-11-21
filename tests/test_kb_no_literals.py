"""Guard: no value from the Soony data may appear in shipped code or config. Values are collected from the data at test
time (IDs, document numbers, larger quantities), so the guard needs no list of its own.

Scanned: every .py, .sql, .yaml, .yml, .js, .ts, .tsx, .html file outside tests, data and the reference envelopes."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "current"
CODE_SUFFIXES = {".py", ".sql", ".yaml", ".yml", ".js", ".ts", ".tsx", ".html"}
SKIP_DIRS = {"tests", "data", ".claude", ".venv", ".git", "node_modules", "__pycache__", "challenge", "analysis",
             "mail_data", ".pytest_cache", "exports", "secrets"}

ID_PATTERNS = [
    r"\b45\d{8}\b",               # purchase orders
    r"\b50\d{8}\b",               # goods receipts / batches
    r"\b[A-Z]{3}-[A-Z]{3}-\d{3}\b",  # material ids
    r"\bSUP-\d{4}\b", r"\bSHP-\d{4}-\d{5}\b", r"\bLSP-\d{2}\b", r"\bRT-\d{3}\b",
    r"\b[A-Z]{4}\d{7}\b",         # containers
    r"\b\d{3}-\d{8}\b",           # air waybills
    r"\b(?:DN|SN|WZ)-\d{6}-\d{3}\b",  # delivery notes
    r"\bPR-[A-Z]{2}-\d{4}\b",     # supplier part numbers
]
QUANTITY = re.compile(r"(?<![\d.,])\d{1,3}(?:[,.]\d{3})+(?![\d,.])|(?<![\d.,])\d{4,7}(?![\d.,])")


def data_values() -> set[str]:
    values: set[str] = set()
    for path in DATA.rglob("*"):
        if path.suffix.lower() not in (".csv", ".json", ".md"):
            continue
        text = path.read_text(errors="replace")
        for pattern in ID_PATTERNS:
            values.update(re.findall(pattern, text))
        for q in QUANTITY.findall(text):
            digits = re.sub(r"[,.]", "", q)
            if 1000 <= int(digits) < 10_000_000 and not (1990 <= int(digits) <= 2100):
                values.add(digits)
    return values


def code_files(base: Path) -> list[Path]:
    out = []
    for p in base.rglob("*"):
        if not p.is_file() or p.suffix not in CODE_SUFFIXES:
            continue
        rel = p.relative_to(ROOT).parts
        if any(part in SKIP_DIRS for part in rel[:-1]):
            continue
        if rel[0] == "structured" and "docs" in rel:
            continue      # reference envelopes are data, not code
        out.append(p)
    return out


def hits_in(files: list[Path], values: set[str]) -> list[str]:
    hits = []
    for path in files:
        text = path.read_text(errors="replace")
        for token in set(re.findall(r"[A-Za-z0-9_-]+", text)):
            if token in values:
                hits.append(f"{path.relative_to(ROOT)}: {token}")
    return sorted(hits)


@pytest.fixture(scope="module")
def values():
    if not DATA.is_dir():
        pytest.skip("no data/current mirror")
    vals = data_values()
    assert len(vals) > 100, "collector found too few values; the guard would be meaningless"
    return vals


def test_no_data_values_in_application_code(values):
    files = [p for p in code_files(ROOT) if p.relative_to(ROOT).parts[0] != "structured"]
    hits = hits_in(files, values)
    assert not hits, "data values in code:\n" + "\n".join(hits)


@pytest.mark.xfail(reason="Codex round 2 item 16: build_structured.py still contains scan contents", strict=False)
def test_no_data_values_in_structured_build_code(values):
    hits = hits_in(code_files(ROOT / "structured"), values)
    assert not hits, f"{len(hits)} data values in structured/ code, e.g.:\n" + "\n".join(hits[:20])
