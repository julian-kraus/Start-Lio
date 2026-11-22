"""The extraction contract lists every attribute the reference envelopes record, and only ever grows.

Messages name fact types and attributes, never values from the data."""

import json
import subprocess
from collections import defaultdict
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = "structured/contract/fact_types.json"

# Added by issue 127: fact type -> (number attributes, string attributes).
ADDED = {
    "supplier": ("lat lon",
                 "category city contact_name payment_terms phone preferred_language status supplier_since"),
    "supplier_part": ("", "category currency description"),
    "component": ("", "abc_class"),
    "product": ("day_one_qty std_cost_eur weekly_plan_qty", "name plants product_line valid_from"),
    "change_record": ("share", "child_id material_id note parent_id primary supplier_id"),
    "goods_receipt": ("", "plant"),
    "po_line": ("", "note plant"),
    "packout": ("", "po_line site_id"),
    "part_xref": ("lead_time_days moq price_per_material", ""),
    "plan_edition": ("", "name"),
    "plan_part": ("per_collector_s per_standard", "description"),
    "price_change": ("current_price", "description"),
    "shipment": ("", "dest_site last_lat last_lon via"),
    "shipment_qty": ("", "eta etd mode note status supplier_id"),
    "stock_batch": ("", "safety_stock site_id weeks_of_cover"),
    "x_workbook_row": ("", "note"),
}
EXPECTED = sorted((fact_type, name, kind)
                  for fact_type, groups in ADDED.items()
                  for kind, names in zip(("number", "string"), groups)
                  for name in names.split())

# The same attribute under another name in a source: not a contract name of its own.
ALIASES = {("po_line", "werks"): "plant"}


def by_name(text: str) -> dict:
    return {t["name"]: t for t in json.loads(text)["fact_types"]}


def git(*args: str) -> str | None:
    try:
        done = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    except FileNotFoundError:
        return None
    return done.stdout if done.returncode == 0 else None


@pytest.fixture(scope="module")
def contract():
    return by_name((ROOT / CONTRACT).read_text())


@pytest.fixture(scope="module")
def recorded():
    values = defaultdict(list)
    for path in sorted((ROOT / "structured" / "docs").glob("*.json")):
        for fact in json.loads(path.read_text())["facts"]:
            values[(fact["fact_type"], fact["attribute"])].append(fact["value"])
    assert values, "no reference facts found"
    return values


@pytest.mark.parametrize("fact_type,name,kind", EXPECTED)
def test_added_attribute_is_listed_with_type_and_description(contract, fact_type, name, kind):
    spec = contract[fact_type]["attributes"].get(name)
    assert spec, f"{fact_type}.{name} is not in the contract"
    assert spec["type"] == kind
    assert spec["description"] not in ("", name), "needs a plain-English description"


@pytest.mark.parametrize("fact_type,name,kind", EXPECTED)
def test_added_type_follows_the_reference(recorded, fact_type, name, kind):
    values = recorded[(fact_type, name)]
    assert values, f"{fact_type}.{name} is not in the reference"
    numeric = all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values)
    assert kind == ("number" if numeric else "string")


def test_nothing_on_main_is_removed_renamed_or_retyped(contract):
    # The fork point, not the tip: a branch that is merely behind main has removed nothing.
    base = git("merge-base", "HEAD", "origin/main") or git("merge-base", "HEAD", "main")
    if not base:
        pytest.skip("no main branch to compare with")
    text = git("show", f"{base.strip()}:{CONTRACT}")
    assert text, "the contract is missing on main"
    changed = []
    for fact_type, before in by_name(text).items():
        now = contract.get(fact_type)
        if now is None or now["subject_keys"] != before["subject_keys"]:
            changed.append(fact_type)
            continue
        changed += [f"{fact_type}.{name}" for name, spec in before["attributes"].items()
                    if now["attributes"].get(name, {}).get("type") != spec["type"]]
    assert not changed, f"changed against main: {changed}"


def test_every_reference_attribute_is_in_the_contract_or_an_alias(contract, recorded):
    unlisted = set()
    for fact_type, name in recorded:
        spec = contract.get(fact_type, {"attributes": {}, "subject_keys": []})
        # An identifier is declared as a subject key and recorded as an attribute as well.
        if name not in spec["attributes"] and name not in spec["subject_keys"]:
            unlisted.add((fact_type, name))
    assert not unlisted - set(ALIASES)


def test_alias_targets_are_contract_attributes(contract):
    for (fact_type, alias), target in ALIASES.items():
        assert target in contract[fact_type]["attributes"]
        assert alias not in contract[fact_type]["attributes"], f"{fact_type}.{alias} is an alias of {target}"
