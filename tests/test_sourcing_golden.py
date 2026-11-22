"""Scenario A ("no action") on the real facts, local only.

Nothing expected is written here: the numbers are read at test time from the hand analysis, and the test skips
without the data mirror (data/current, or TRACKER_DATA_DIR/current)."""

import hashlib
import os
import re
from datetime import timedelta, timezone, datetime, time
from pathlib import Path

import pytest
import yaml

from kb import engine, loader
from kb.facts import fact_id
from plan import contracts
from sourcing import coverage, coverage_windows
from sourcing.facts import Facts, day, number
from test_sourcing_coverage import check_sources

ROOT = Path(__file__).resolve().parent.parent
MIRROR = Path(os.environ.get("TRACKER_DATA_DIR", ROOT / "data")) / "current"
ANALYSIS = ROOT / "analysis" / "data-overview.md"
NUMBER = r"-?\d[\d,]*"
pytestmark = pytest.mark.skipif(not MIRROR.is_dir() or not ANALYSIS.is_file(), reason="no local data mirror")


def real_facts() -> Facts:
    current = {str(p.relative_to(MIRROR)): hashlib.sha256(p.read_bytes()).hexdigest()
               for p in MIRROR.rglob("*") if p.is_file()}
    rows, parsed = {}, {}
    for path in sorted((ROOT / "structured" / "docs").glob("*.json")):
        env = loader.read_envelope(path)
        if not env or current.get(env["source"]["path"]) != env["source"]["sha256"]:
            continue
        for item in loader.envelope_facts(env, loader.DEFAULT_CONFIDENCE):
            key = fact_id(item)
            as_of = None if item["as_of"] is None else str(item["as_of"])
            rows[key] = {**item, "fact_id": key, "path": item["source_path"], "as_of": as_of}
            parsed[key] = engine.Fact(key, item["fact_type"], item["subject"], item["attribute"], item["value"],
                                      as_of, item["confidence"], item["extractor"], item["source_path"],
                                      item["source_sha256"], item["evidence"], item["unit"], env.get("doc_kind"))
    seed = yaml.safe_load((ROOT / "kb" / "rules.seed.yaml").read_text())
    launch = max(d for r in Facts(rows.values()).rows("product") if (d := day(r.get("launch_date"))))
    now = datetime.combine(launch, time(), timezone.utc)
    believed, issues, _ = engine.evaluate(seed["rules"], engine.build_records(list(parsed.values())),
                                          seed["settings"], now)
    return Facts(rows.values(), [vars(b) for b in believed], [vars(i) for i in issues])


@pytest.fixture(scope="module")
def scenario():
    facts = real_facts()
    zone = timezone(timedelta(hours=1))
    launch = max(d for r in facts.rows("product") if (d := day(r.get("launch_date"))))
    end_of_day = datetime.combine(launch, time(23, 59), zone)
    windows = coverage_windows.pack_windows(facts, end_of_day, end_of_day)
    result = coverage.compute(facts, now=windows[0].start, deadline=windows[-1].end)
    return facts, result


def table_row(text: str, start: str) -> list[str]:
    line = next(line for line in text.splitlines() if line.startswith(start))
    return [c.strip() for c in line.strip("|").split("|")]


def test_scenario_a_gap_and_need_per_part(scenario):
    _, result = scenario
    table = ANALYSIS.read_text().split("### 3.2 Per component")[1].split("### 3.3")[0]
    phantom = {p["material_id"] for p in result["not_shortages"]}
    assert phantom and result["parts"]
    for part in result["parts"]:
        cells = table_row(table, f"| {part['material_id']} ")
        need = number(re.findall(NUMBER, cells[1])[-1])
        # where the analysis lists variants, the first is "no action, released revision, pick-up done"
        cover = number(re.search(rf"[+-]?{NUMBER}", cells[-1]).group().lstrip("+"))
        assert (part["need"]["value"], part["gap"]["value"]) == (need, max(0, -cover)), part["material_id"]
    for material in phantom:
        assert number(re.search(NUMBER, table_row(table, f"| {material} ")[1]).group()) == 0


def test_scenario_a_packable_copies(scenario):
    _, result = scenario
    row = table_row(ANALYSIS.read_text(), "| A. No action ")
    expected = [number(re.findall(NUMBER, cell)[-1]) for cell in (row[1], row[3])]
    covered = {e["product_id"]: e["covered"]["value"] for e in result["editions"]}
    assert [covered[p] for p in result["priority"]["order"]] == expected


def test_scenario_a_validates_and_every_figure_has_a_real_fact(scenario):
    facts, result = scenario
    contracts.validate("coverage", result)
    check_sources(result, set(facts.by_id))
