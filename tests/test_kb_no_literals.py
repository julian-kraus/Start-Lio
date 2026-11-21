"""Guard: no value from the Soony data may appear in kb's code or rule seed. Values are collected from the data at
test time (IDs, document numbers, larger quantities), so the guard needs no list of its own."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "current"
CODE = [p for p in (ROOT / "kb").rglob("*") if p.suffix in (".py", ".sql", ".yaml")]

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


@pytest.mark.skipif(not DATA.is_dir(), reason="no data/current mirror")
def test_no_data_values_in_kb_code():
    values = data_values()
    assert len(values) > 100, "collector found too few values; the guard would be meaningless"
    hits = []
    for path in CODE:
        text = path.read_text()
        for token in set(re.findall(r"[A-Za-z0-9_-]+", text)):
            if token in values:
                hits.append(f"{path.relative_to(ROOT)}: {token}")
    assert not hits, "data values in code:\n" + "\n".join(sorted(hits))
