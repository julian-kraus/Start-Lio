"""Tests for the scenario event catalog (issue #183 / L1).

Pure-Python tests (schema, coverage, rendering, README) run anywhere. The reference-resolution tests build a
throwaway kb + plan database with synthetic facts and run `lab.catalog.load()` against it, the same way the
scenario lab (L0) seeds it. Synthetic values only; `tests/` is outside the literal guard.
"""

from __future__ import annotations

import os
import uuid

import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from lab import catalog

URL = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")


# --------------------------------------------------------------------------- schema and coverage (no database)

def test_catalog_loads_with_at_least_forty_types_across_the_required_families():
    cat = catalog.load_catalog()
    assert len(cat.types) >= 40
    families = set(cat.families())
    required = {
        "offers", "confirmations", "delays", "deliveries", "cancellations", "silence", "proof", "quality",
        "terms", "capacity", "logistics", "demand", "policy", "conflicts", "noise",
    }
    assert required <= families, required - families
    channels = {t.channel for t in cat.types}
    assert channels == set(catalog.CHANNELS), channels
    used_refs = {r for t in cat.types for r in t.refs}
    assert used_refs == set(catalog.REFERENCES), used_refs


def test_every_type_declares_id_family_channel_params_produces_and_expected_changes():
    cat = catalog.load_catalog()
    seen = set()
    for t in cat.types:
        assert t.id not in seen, f"duplicate {t.id}"
        seen.add(t.id)
        assert t.id.startswith(f"{t.family}."), t.id
        assert t.description.strip()
        assert t.channel in catalog.CHANNELS
        assert t.produces.get(t.channel), f"{t.id}: produces has no {t.channel} block"
        assert t.expects, f"{t.id}: no expected fact changes"
        for e in t.expects:
            assert e["fact_type"] and e["change"] in catalog.CHANGES
        for p in t.params.values():
            assert p.type in catalog.PARAM_TYPES
            if p.type == "ref":
                assert p.ref in catalog.REFERENCES


def test_required_languages_are_covered_by_the_mail_templates():
    cat = catalog.load_catalog()
    assert set(catalog.REQUIRED_LANGS) <= cat.languages(), cat.languages()


def test_mail_templates_render_in_the_supplier_language_with_english_fallback():
    cat = catalog.load_catalog().by_id()
    multilingual = cat["delays.new_eta"]  # has en + de

    def resolved_for(language):
        return {
            "supplier": [{"supplier_id": "SUP-TEST-A", "language": language, "persona": "persona-a"}],
            "order_line": [{"po_number": "PO-TEST-0001", "po_line": "10"}],
        }

    de = catalog.render_example(multilingual, resolved_for("de"))
    assert de["language"] == "de" and "Liefertermin" in de["subject"]
    en = catalog.render_example(multilingual, resolved_for("en"))
    assert en["language"] == "en" and "delivery date" in en["subject"].lower()
    # A supplier language with no template for this type falls back to English.
    fr = catalog.render_example(multilingual, resolved_for("fr"))
    assert fr["language"] == "en"
    # A type that only has English also falls back for a non-English supplier.
    only_en = cat["confirmations.partial_confirm"]
    got = catalog.render_example(only_en, resolved_for("pl"))
    assert got["language"] == "en"


def test_every_type_renders_one_example_with_no_unfilled_placeholders():
    cat = catalog.load_catalog()
    resolved = _synthetic_bindings()
    for t in cat.types:
        example = catalog.render_example(t, resolved)
        assert example["id"] == t.id and example["channel"] == t.channel
        rendered = " ".join(str(v) for v in _leaf_strings(example))
        assert "{" not in rendered and "}" not in rendered, f"{t.id}: unfilled placeholder in {rendered}"
        if t.channel == "mail":
            assert example["subject"].strip() and example["body"].strip()
        elif t.channel == "document":
            assert example["filename"].strip() and example["kind"]
        elif t.channel == "table":
            assert example["path"].strip() and example["row"]
        elif t.channel == "clock":
            assert example["move"].strip()


def test_readme_is_generated_from_the_catalog_and_committed_in_sync():
    cat = catalog.load_catalog()
    on_disk = (catalog.CATALOG_DIR / "README.md").read_text()
    assert on_disk == catalog.render_readme(cat), "run `python -m lab.catalog readme` and commit the result"


@pytest.mark.parametrize("mutate, needle", [
    (lambda d: d["types"][0].pop("id"), "id"),
    (lambda d: d["types"][0].update(channel="smoke"), "channel"),
    (lambda d: d["types"][0].update(id="delays.ghost", parameters={"x": {"type": "ref", "ref": "nope"}}), "nope"),
    (lambda d: d["types"][0]["produces"]["mail"].update(body={"de": "x"}), "en"),
    (lambda d: d["types"][0].update(expects=[]), "expects"),
    (lambda d: d["types"][0]["produces"]["mail"].update(subject="{missing} today"), "missing"),
])
def test_invalid_types_fail_validation_naming_the_problem(tmp_path, mutate, needle):
    import yaml

    doc = yaml.safe_load((catalog.CATALOG_DIR / "delays.yaml").read_text())
    mutate(doc)
    (tmp_path / "delays.yaml").write_text(yaml.safe_dump(doc))
    with pytest.raises(catalog.CatalogError) as err:
        catalog.load_catalog(tmp_path)
    assert needle in str(err.value)


# --------------------------------------------------------------------------- reference resolution (database)

@needs_db
def test_load_resolves_every_reference_and_renders_against_a_seeded_database():
    kb_schema = f"kb_cat_{uuid.uuid4().hex[:8]}"
    plan_schema = f"plan_cat_{uuid.uuid4().hex[:8]}"
    conn = _seed(kb_schema, plan_schema, editions=True)
    try:
        cat = catalog.load(conn, kb_schema=kb_schema, plan_schema=plan_schema)
        assert len(cat.types) >= 40
        resolved = catalog.resolve_references(conn, kb_schema, plan_schema)
        for ref in catalog.REFERENCES:
            assert resolved[ref], f"{ref} did not resolve"
        # The language seeded on the supplier flows into a mail example.
        new_eta = cat.by_id()["delays.new_eta"]
        assert catalog.render_example(new_eta, resolved)["language"] == "de"
    finally:
        _drop(conn, kb_schema, plan_schema)


@needs_db
def test_load_raises_naming_the_types_when_a_reference_cannot_resolve():
    kb_schema = f"kb_cat_{uuid.uuid4().hex[:8]}"
    plan_schema = f"plan_cat_{uuid.uuid4().hex[:8]}"
    conn = _seed(kb_schema, plan_schema, editions=False)  # no goals -> edition unresolvable
    try:
        with pytest.raises(catalog.CatalogError) as err:
            catalog.load(conn, kb_schema=kb_schema, plan_schema=plan_schema)
        message = str(err.value)
        assert "edition" in message and "demand.preorders_up" in message
    finally:
        _drop(conn, kb_schema, plan_schema)


# --------------------------------------------------------------------------- helpers

def _synthetic_bindings() -> dict:
    return {
        "supplier": [{"supplier_id": "SUP-TEST-A", "language": "de", "persona": "persona-a"}],
        "part": [{"material_id": "MAT-TEST-1", "supplier_part_no": "SPN-TEST-1"}],
        "order_line": [{"po_number": "PO-TEST-0001", "po_line": "10"}],
        "edition": [{"edition": "edition-one"}],
        "kb_id": [{"fact_id": uuid.uuid4().hex}],
    }


def _leaf_strings(node):
    if isinstance(node, dict):
        for v in node.values():
            yield from _leaf_strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _leaf_strings(v)
    else:
        yield node


def _fact(conn, fact_type, subject, attribute, value):
    conn.execute(
        "INSERT INTO facts (fact_id, fact_type, subject, attribute, value, extractor, confidence, "
        "source_path, source_sha256) VALUES (%s, %s, %s, %s, %s, 'test', 1.0, 'seed', 'seed')",
        (uuid.uuid4().hex, fact_type, Jsonb(subject), attribute, Jsonb(value)))


def _seed(kb_schema, plan_schema, *, editions):
    import kb.db as kbdb
    from plan import db as plandb

    conn = kbdb.connect(URL, kb_schema)
    kbdb.migrate(conn, kb_schema)
    _fact(conn, "supplier", {"supplier_id": "SUP-TEST-A"}, "name", "Synthetic A")
    _fact(conn, "supplier", {"supplier_id": "SUP-TEST-A"}, "preferred_language", "de")
    _fact(conn, "part_xref", {"material_id": "MAT-TEST-1", "supplier_part_no": "SPN-TEST-1"}, "supplier_part_no", "SPN-TEST-1")
    _fact(conn, "confirmed_date", {"po_number": "PO-TEST-0001", "po_line": "10"}, "eta", "soon")
    conn.commit()
    plandb.migrate(conn, plan_schema)
    if editions:
        conn.execute(sql.SQL("INSERT INTO {}.goals (statement, edition) VALUES (%s, %s)").format(
            sql.Identifier(plan_schema)), ("synthetic goal", "edition-one"))
    conn.commit()
    conn.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(kb_schema)))
    return conn


def _drop(conn, *schemas):
    for schema in schemas:
        conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
    conn.commit()
    conn.close()
