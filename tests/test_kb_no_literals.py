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


def distinctive_quantity(match: str) -> str | None:
    """Digits of a quantity worth guarding, or None if the number is too generic to attribute to the data.

    Dropped: things outside 1000..9,999,999, four-digit years, and generic round thousands below ten thousand
    (1000, 2000, ... 9000). Those small round numbers are everyday code constants (buffer sizes, char limits,
    token budgets), so flagging them is a false positive rather than a leaked data value. Everything from five
    digits up, and every non-round four-digit number, is still collected."""
    digits = re.sub(r"[,.]", "", match)
    n = int(digits)
    if not 1000 <= n < 10_000_000:
        return None
    if 1990 <= n <= 2100:
        return None
    if n < 10_000 and n % 1000 == 0:
        return None
    return digits


def collect_from_text(text: str) -> set[str]:
    values: set[str] = set()
    for pattern in ID_PATTERNS:
        values.update(re.findall(pattern, text))
    for q in QUANTITY.findall(text):
        digits = distinctive_quantity(q)
        if digits is not None:
            values.add(digits)
    return values


def data_values() -> set[str]:
    values: set[str] = set()
    for path in DATA.rglob("*"):
        if path.suffix.lower() not in (".csv", ".json", ".md"):
            continue
        values |= collect_from_text(path.read_text(errors="replace"))
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


def label(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return path.name


def hits_in(files: list[Path], values: set[str]) -> list[str]:
    hits = []
    for path in files:
        text = path.read_text(errors="replace")
        for token in set(re.findall(r"[A-Za-z0-9_-]+", text)):
            if token in values:
                hits.append(f"{label(path)}: {token}")
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


def test_no_data_values_in_structured_build_code(values):
    hits = hits_in(code_files(ROOT / "structured"), values)
    assert not hits, f"{len(hits)} data values in structured/ code, e.g.:\n" + "\n".join(hits[:20])


def test_generic_round_thousands_not_collected():
    # The free-text "4,000 pcs" in the data collides with everyday code constants (buffer and char limits),
    # so a generic round thousand is not collected. Distinctive quantities and ids still are.
    text = "ship 54321 units against PO 4512345678; sleeve bag holds 4,000 pcs; booked 2026-11-21"
    collected = collect_from_text(text)
    assert "4000" not in collected       # the current false positive, fixed
    assert "2026" not in collected       # year, not a quantity
    assert "54321" in collected          # five-digit quantity still collected
    assert "4512345678" in collected     # purchase order still collected
    assert distinctive_quantity("4000") is None
    assert distinctive_quantity("9000") is None
    assert distinctive_quantity("10000") == "10000"   # five digits: kept even when round
    assert distinctive_quantity("10500") == "10500"
    assert distinctive_quantity("4321") == "4321"     # four digits, not round: kept


def test_guard_still_catches_planted_literals(tmp_path):
    # A planted purchase order (id pattern) and a distinctive five-digit quantity in a scanned file are flagged.
    leak = tmp_path / "leak.py"
    leak.write_text("DEFAULT_PO = '4512345678'\nREORDER_POINT = 54321\n")
    vals = collect_from_text("PO 4512345678 for 54321 units")
    hits = hits_in([leak], vals)
    assert hits == ["leak.py: 4512345678", "leak.py: 54321"]
