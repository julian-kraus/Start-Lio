"""Load and validate the scenario event catalog: a typed list of supplier and world events the lab can inject.

Each event enters only through a real input channel (supplier mail from a test persona, a document file, a
table row change, or a clock move), never by writing believed values or Lookahead records directly. A type
describes what it produces as a template with placeholders; concrete values (supplier, part, order line,
edition) are references resolved from kb facts on the seeded lab database at run time, so no portal value
lives in the catalog. `load_catalog()` parses and schema-checks the YAML (no database); `load(conn)` also
resolves every reference against the seeded database and renders one example per type, raising `CatalogError`
naming the offending type id; `render_readme()` renders `lab/catalog/README.md`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

CATALOG_DIR = Path(__file__).resolve().parent / "catalog"

CHANNELS = ("mail", "document", "table", "clock")
REFERENCES = ("supplier", "part", "order_line", "edition", "kb_id")
PARAM_TYPES = ("ref", "text", "date", "number", "enum", "money", "duration")
CHANGES = ("new", "update", "deactivate", "conflict", "none")
# At least these four must appear across the catalog; real supplier mail is not all English.
REQUIRED_LANGS = ("en", "de", "pl", "zh")
KNOWN_LANGS = ("en", "de", "pl", "zh", "ko")
# Fields a reference binding exposes, so `{param.field}` placeholders can be checked at load time.
REF_FIELDS = {
    "supplier": ("supplier_id", "language", "persona"),
    "part": ("material_id", "supplier_part_no"),
    "order_line": ("po_number", "po_line"),
    "edition": ("edition",),
    "kb_id": ("fact_id",),
}
PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)(?:\.([a-z_][a-z0-9_]*))?\}")
ID_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

class CatalogError(ValueError):
    """A catalog type failed validation. The message starts with the type id."""

@dataclass(frozen=True)
class Param:
    name: str
    type: str
    required: bool = True
    ref: str | None = None
    values: tuple[str, ...] = ()

@dataclass(frozen=True)
class EventType:
    id: str
    family: str
    description: str
    channel: str
    params: dict[str, Param]
    produces: dict
    expects: tuple[dict, ...]
    source: str

    @property
    def refs(self) -> set[str]:
        return {p.ref for p in self.params.values() if p.type == "ref"}

@dataclass(frozen=True)
class Catalog:
    types: tuple[EventType, ...]

    def by_id(self) -> dict[str, EventType]:
        return {t.id: t for t in self.types}

    def families(self) -> list[str]:
        seen: list[str] = []
        for t in self.types:
            if t.family not in seen:
                seen.append(t.family)
        return seen

    def languages(self) -> set[str]:
        """Every mail language that appears anywhere in the catalog, for the EN/DE/PL/ZH coverage check."""
        langs: set[str] = set()
        for t in self.types:
            langs |= _languages_of(t.produces.get("mail", {}))
        return langs

def load_catalog(catalog_dir: Path | None = None) -> Catalog:
    """Parse and schema-check every `*.yaml` file under the catalog directory. Raises CatalogError."""
    directory = catalog_dir or CATALOG_DIR
    files = sorted(directory.glob("*.yaml"))
    if not files:
        raise CatalogError(f"no catalog files under {directory}")
    types: list[EventType] = []
    seen_ids: dict[str, str] = {}
    for path in files:
        doc = yaml.safe_load(path.read_text()) or {}
        family = doc.get("family")
        if not isinstance(family, str) or not NAME_RE.match(family):
            raise CatalogError(f"{path.name}: 'family' must be a lower_snake slug, got {family!r}")
        entries = doc.get("types")
        if not isinstance(entries, list) or not entries:
            raise CatalogError(f"{path.name}: 'types' must be a non-empty list")
        for raw in entries:
            event = _parse_type(raw, family, path.name)
            if event.id in seen_ids:
                raise CatalogError(f"{event.id}: duplicate id, also in {seen_ids[event.id]}")
            seen_ids[event.id] = path.name
            types.append(event)
    return Catalog(tuple(types))

def _parse_type(raw: object, family: str, source: str) -> EventType:
    if not isinstance(raw, dict):
        raise CatalogError(f"{source}: each type must be a mapping, got {type(raw).__name__}")
    tid = raw.get("id")
    where = tid if isinstance(tid, str) else f"{source}:<missing id>"
    if not isinstance(tid, str) or not ID_RE.match(tid):
        raise CatalogError(f"{where}: 'id' must look like '<family>.<slug>'")
    if not tid.startswith(f"{family}."):
        raise CatalogError(f"{tid}: id must start with its file family '{family}.'")
    description = raw.get("description")
    if not isinstance(description, str) or not description.strip():
        raise CatalogError(f"{tid}: 'description' is required and must be non-empty")
    channel = raw.get("channel")
    if channel not in CHANNELS:
        raise CatalogError(f"{tid}: 'channel' must be one of {CHANNELS}, got {channel!r}")
    params = _parse_params(tid, raw.get("parameters", {}))
    produces = raw.get("produces")
    if not isinstance(produces, dict) or not produces:
        raise CatalogError(f"{tid}: 'produces' must be a non-empty mapping")
    expects = _parse_expects(tid, raw.get("expects"))
    event = EventType(tid, family, description.strip(), channel, params, produces, expects, source)
    _check_produces(event)
    _check_placeholders(event)
    return event

def _parse_params(tid: str, raw: object) -> dict[str, Param]:
    if not isinstance(raw, dict):
        raise CatalogError(f"{tid}: 'parameters' must be a mapping")
    params: dict[str, Param] = {}
    for name, spec in raw.items():
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise CatalogError(f"{tid}: parameter name {name!r} must be a lower_snake slug")
        if not isinstance(spec, dict):
            raise CatalogError(f"{tid}.{name}: parameter spec must be a mapping")
        ptype = spec.get("type")
        if ptype not in PARAM_TYPES:
            raise CatalogError(f"{tid}.{name}: 'type' must be one of {PARAM_TYPES}, got {ptype!r}")
        ref = spec.get("ref")
        values = spec.get("values", [])
        if ptype == "ref":
            if ref not in REFERENCES:
                raise CatalogError(f"{tid}.{name}: ref must be one of {REFERENCES}, got {ref!r}")
        elif ref is not None:
            raise CatalogError(f"{tid}.{name}: 'ref' is only valid for type 'ref'")
        if ptype == "enum":
            if not isinstance(values, list) or not all(isinstance(v, str) for v in values) or not values:
                raise CatalogError(f"{tid}.{name}: enum needs a non-empty list of string 'values'")
        elif values:
            raise CatalogError(f"{tid}.{name}: 'values' is only valid for type 'enum'")
        params[name] = Param(name, ptype, bool(spec.get("required", True)), ref, tuple(values))
    return params

def _parse_expects(tid: str, raw: object) -> tuple[dict, ...]:
    if not isinstance(raw, list) or not raw:
        raise CatalogError(f"{tid}: 'expects' must be a non-empty list of expected fact changes")
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            raise CatalogError(f"{tid}: each 'expects' item must be a mapping")
        fact_type = item.get("fact_type")
        if not isinstance(fact_type, str) or not fact_type.strip():
            raise CatalogError(f"{tid}: an 'expects' item is missing 'fact_type'")
        change = item.get("change")
        if change not in CHANGES:
            raise CatalogError(f"{tid}: 'change' must be one of {CHANGES}, got {change!r}")
        attribute = item.get("attribute")
        if attribute is not None and not isinstance(attribute, str):
            raise CatalogError(f"{tid}: 'attribute' must be a string when present")
        out.append({"fact_type": fact_type, "attribute": attribute, "change": change})
    return tuple(out)

def _check_produces(event: EventType) -> None:
    block = event.produces.get(event.channel)
    if not isinstance(block, dict):
        raise CatalogError(f"{event.id}: 'produces' must hold a '{event.channel}' mapping for this channel")
    if event.channel == "mail":
        if not _supplier_param(event):
            raise CatalogError(f"{event.id}: a mail event needs a parameter of ref 'supplier' to address it")
        for part in ("subject", "body"):
            _check_localised(event, block.get(part), part)
    elif event.channel == "document":
        for key in ("filename", "kind", "body"):
            if not isinstance(block.get(key), str) or not block[key].strip():
                raise CatalogError(f"{event.id}: document 'produces' needs a non-empty '{key}'")
    elif event.channel == "table":
        if not isinstance(block.get("path"), str) or not block["path"].strip():
            raise CatalogError(f"{event.id}: table 'produces' needs a 'path'")
        if not isinstance(block.get("row"), dict) or not block["row"]:
            raise CatalogError(f"{event.id}: table 'produces' needs a non-empty 'row' mapping")
    elif event.channel == "clock":
        if not isinstance(block.get("move"), str) or not block["move"].strip():
            raise CatalogError(f"{event.id}: clock 'produces' needs a 'move' target")

def _check_localised(event: EventType, value: object, part: str) -> None:
    if isinstance(value, str):
        if not value.strip():
            raise CatalogError(f"{event.id}: mail '{part}' is empty")
        return
    if isinstance(value, dict):
        if "en" not in value:
            raise CatalogError(f"{event.id}: mail '{part}' must include an 'en' fallback")
        for lang, text in value.items():
            if lang not in KNOWN_LANGS:
                raise CatalogError(f"{event.id}: mail '{part}' has unknown language {lang!r}")
            if not isinstance(text, str) or not text.strip():
                raise CatalogError(f"{event.id}: mail '{part}' for {lang!r} is empty")
        return
    raise CatalogError(f"{event.id}: mail '{part}' must be a string or a language map")

def _supplier_param(event: EventType) -> Param | None:
    for p in event.params.values():
        if p.type == "ref" and p.ref == "supplier":
            return p
    return None

def _check_placeholders(event: EventType) -> None:
    for text in _template_strings(event.produces):
        for name, field in PLACEHOLDER.findall(text):
            param = event.params.get(name)
            if param is None:
                raise CatalogError(f"{event.id}: template placeholder '{{{name}}}' is not a declared parameter")
            if field:
                if param.type != "ref":
                    raise CatalogError(f"{event.id}: '{{{name}.{field}}}' but '{name}' is not a reference")
                if field not in REF_FIELDS[param.ref]:
                    raise CatalogError(f"{event.id}: reference '{name}' ({param.ref}) has no field '{field}'")
            elif param.type == "ref":
                first = REF_FIELDS[param.ref][0]
                raise CatalogError(f"{event.id}: reference '{name}' needs a field, e.g. '{{{name}.{first}}}'")

def _template_strings(node: object):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _template_strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _template_strings(v)

def resolve_references(conn, kb_schema: str = "kb", plan_schema: str = "plan") -> dict[str, list[dict]]:
    """Concrete candidate bindings per reference kind, read from the seeded database. A kind is resolvable
    when it has at least one candidate; the caller decides which kinds the catalog actually needs."""
    from psycopg import sql

    def rows(query: str, schema: str) -> list[dict]:
        return [dict(r) for r in conn.execute(sql.SQL(query).format(s=sql.Identifier(schema)))]

    out: dict[str, list[dict]] = {}
    out["supplier"] = rows(
        "SELECT DISTINCT ON (s.subject->>'supplier_id') s.subject->>'supplier_id' AS supplier_id, "
        "COALESCE(l.value #>> '{{}}', 'en') AS language FROM {s}.facts s "
        "LEFT JOIN {s}.facts l ON l.valid AND l.fact_type = 'supplier' "
        "AND l.attribute = 'preferred_language' AND l.subject->>'supplier_id' = s.subject->>'supplier_id' "
        "WHERE s.valid AND s.fact_type = 'supplier' AND s.subject ? 'supplier_id' "
        "ORDER BY s.subject->>'supplier_id'", kb_schema)
    personas = _personas(conn) if _has_table(conn, "mail", "contacts") else {}
    for r in out["supplier"]:
        r["persona"] = personas.get(r["supplier_id"]) or r["supplier_id"]
    out["part"] = rows(
        "SELECT DISTINCT ON (material_id) subject->>'material_id' AS material_id, "
        "subject->>'supplier_part_no' AS supplier_part_no "
        "FROM {s}.facts WHERE valid AND subject ? 'material_id' ORDER BY material_id", kb_schema)
    out["order_line"] = rows(
        "SELECT DISTINCT subject->>'po_number' AS po_number, subject->>'po_line' AS po_line "
        "FROM {s}.facts WHERE valid AND subject ? 'po_number' AND subject ? 'po_line'", kb_schema)
    out["kb_id"] = rows("SELECT fact_id FROM {s}.facts WHERE valid ORDER BY fact_id LIMIT 1", kb_schema)
    out["edition"] = rows(
        "SELECT DISTINCT edition FROM {s}.goals WHERE edition IS NOT NULL", plan_schema
    ) if _has_table(conn, plan_schema, "goals") else []
    return out

def _personas(conn) -> dict[str, str]:
    from psycopg import sql

    stmt = sql.SQL("SELECT supplier_id, persona_id FROM {m}.contacts "
                   "WHERE persona_id IS NOT NULL AND supplier_id IS NOT NULL").format(m=sql.Identifier("mail"))
    try:
        return {r["supplier_id"]: r["persona_id"] for r in conn.execute(stmt)}
    except Exception:
        return {}

def _has_table(conn, schema: str, table: str) -> bool:
    row = conn.execute("SELECT to_regclass(%s) AS reg", (f"{schema}.{table}",)).fetchone()
    return row["reg"] is not None

def render_example(event: EventType, resolved: dict[str, list[dict]]) -> dict:
    """Render the produces template for one type using the first candidate of each reference and a `<name>`
    placeholder for every scalar parameter. Raises CatalogError if a needed reference has no candidate."""
    context: dict[str, str] = {}
    for p in event.params.values():
        if p.type == "ref":
            candidates = resolved.get(p.ref) or []
            if not candidates:
                raise CatalogError(f"{event.id}: reference '{p.ref}' did not resolve on the seeded database")
            binding = candidates[0]
            for fieldname in REF_FIELDS[p.ref]:
                context[f"{p.name}.{fieldname}"] = str(binding.get(fieldname) or f"<{p.name}.{fieldname}>")
        elif p.type == "enum":
            context[p.name] = p.values[0]
        else:
            context[p.name] = f"<{p.name}>"

    def fill(text: str) -> str:
        return PLACEHOLDER.sub(lambda m: context[m.group(1) + ("." + m.group(2) if m.group(2) else "")], text)

    block = dict(event.produces[event.channel])
    example: dict = {"id": event.id, "channel": event.channel}
    if event.channel == "mail":
        supplier = _supplier_param(event)
        lang = context.get(f"{supplier.name}.language", "en")
        available = _languages_of(block) | {"en"}
        example["language"] = lang if lang in available else "en"
        example["persona"] = fill(str(block.get("persona", "{%s.persona}" % supplier.name)))
        example["subject"] = fill(_pick_lang(block.get("subject"), example["language"]))
        example["body"] = fill(_pick_lang(block.get("body"), example["language"]))
    elif event.channel == "document":
        example.update(filename=fill(block["filename"]), kind=block["kind"], body=fill(block["body"]))
    elif event.channel == "table":
        example.update(path=fill(block["path"]), row={k: fill(str(v)) for k, v in block["row"].items()})
    elif event.channel == "clock":
        example["move"] = fill(block["move"])
    return example

def _pick_lang(value: object, lang: str) -> str:
    if isinstance(value, dict):
        return value.get(lang) or value["en"]
    return str(value)

def _languages_of(block: dict) -> set[str]:
    langs: set[str] = set()
    for part in ("subject", "body"):
        if isinstance(block.get(part), dict):
            langs.update(block[part])
    return langs

def load(conn, *, catalog_dir: Path | None = None, kb_schema: str = "kb", plan_schema: str = "plan") -> Catalog:
    """Full validation against the seeded lab database: schema check, every reference resolvable, every
    template renders. Raises CatalogError naming the first offending type id."""
    catalog = load_catalog(catalog_dir)
    resolved = resolve_references(conn, kb_schema, plan_schema)
    used = {ref for t in catalog.types for ref in t.refs}
    for ref in sorted(used):
        if not resolved.get(ref):
            users = ", ".join(t.id for t in catalog.types if ref in t.refs)
            raise CatalogError(f"reference '{ref}' has no candidate on the seeded database; needed by: {users}")
    for event in catalog.types:
        render_example(event, resolved)
    return catalog

def render_readme(catalog: Catalog) -> str:
    """Render the catalog list as Markdown. lab/catalog/README.md is this output, not hand-written."""
    by_family: dict[str, list[EventType]] = {}
    for t in catalog.types:
        by_family.setdefault(t.family, []).append(t)
    lines = [
        "# Scenario event catalog", "",
        "Generated from the YAML files in this directory by `lab.catalog.render_readme`. Do not edit by hand;",
        "run `python -m lab.catalog readme` to regenerate after changing a family file.", "",
        f"{len(catalog.types)} event types in {len(catalog.families())} families. Every value shown at run time",
        "is a reference resolved from kb facts on the seeded lab database; nothing here carries a portal value.",
        "", "| Family | Types |", "| --- | --- |",
    ]
    for family in catalog.families():
        lines.append(f"| `{family}` | {len(by_family[family])} |")
    lines.append("")
    for family in catalog.families():
        lines += [f"## {family}", "", "| id | channel | references | description |", "| --- | --- | --- | --- |"]
        for t in by_family[family]:
            refs = ", ".join(sorted(t.refs)) or "none"
            lines.append(f"| `{t.id}` | {t.channel} | {refs} | {t.description} |")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"

def write_readme(catalog_dir: Path | None = None) -> Path:
    directory = catalog_dir or CATALOG_DIR
    target = directory / "README.md"
    target.write_text(render_readme(load_catalog(directory)))
    return target

if __name__ == "__main__":  # python -m lab.catalog readme
    import sys
    if len(sys.argv) == 2 and sys.argv[1] == "readme":
        print(f"wrote {write_readme()}")
    else:
        cat = load_catalog()
        print(f"{len(cat.types)} types in {len(cat.families())} families: {', '.join(cat.families())}")
