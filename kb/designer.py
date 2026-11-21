"""Structure designer hooks: ask the LLM for a new structure when the registry has none, then check it.

Tables: a proposed schema map is applied at once ("auto") only if it parses the very table it was made for with
no parse errors, entity-ID subjects and at least one row. Otherwise it stays "proposed" for a person.
Documents: a proposed doc kind is always "proposed"; its facts count with capped confidence until promoted.
"""

from __future__ import annotations

import logging

from . import registry
from .check_structured import ROW_ID
from .llm.base import Document, LLMClient, StructureProposal
from .tabular import HEADER_SCAN_ROWS, RawTable, find_header, table_to_facts

log = logging.getLogger(__name__)


def _similar_kinds(conn, limit: int = 5):
    return registry.structures(conn)[:limit]


def propose_table_map(conn, llm: LLMClient, path: str, sha: str, container: str, table: RawTable) -> dict:
    doc = Document(path, sha, container, text="\n".join("\t".join(r) for r in table.rows[:HEADER_SCAN_ROWS + 10]))
    try:
        proposal: StructureProposal = llm.design(doc, _similar_kinds(conn), registry.contract_summary(conn),
                                                 sample_rows=table.rows[:HEADER_SCAN_ROWS + 10])
    except Exception as exc:
        log.warning("designer failed for %s: %s", path, exc)
        return {"sheet": table.sheet, "status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    mapping = proposal.schema_map
    if proposal.target != "schema_map" or not mapping or not mapping.get("signature"):
        return {"sheet": table.sheet, "status": "failed", "error": "designer did not return a schema map"}
    validation = validate_table_map(table, mapping, container, path)
    status = "auto" if validation["ok"] else "proposed"
    registry.upsert_schema_map(conn, mapping, origin="designer", status=status, created_by=llm.label,
                               validation={**validation, "reason": proposal.reason})
    return {"sheet": table.sheet, "status": status, "signature": mapping["signature"], "validation": validation}


def validate_table_map(table: RawTable, mapping: dict, container: str, path: str) -> dict:
    hit = find_header(table, {mapping["signature"]: mapping}, container)
    if hit is None:
        return {"ok": False, "problem": "signature does not match any header row of the table"}
    idx, m = hit
    try:
        facts, stats = table_to_facts(table, idx, m, path=path, sha256="validation")
    except (KeyError, TypeError, ValueError) as exc:
        return {"ok": False, "problem": f"map cannot be applied: {exc}"}
    row_ids = sum(1 for f in facts if any(ROW_ID.match(str(v)) for v in f["subject"].values()))
    ok = stats["rows"] > 0 and stats["parse_errors"] == 0 and row_ids == 0
    return {"ok": ok, "rows": stats["rows"], "parse_errors": stats["parse_errors"], "row_id_subjects": row_ids,
            "error_samples": stats["error_samples"][:3]}


def propose_doc_kind(conn, llm: LLMClient, doc: Document) -> str | None:
    """Returns the name of the proposed doc kind, or None if the designer could not propose one."""
    try:
        proposal = llm.design(doc, _similar_kinds(conn), registry.contract_summary(conn))
    except Exception as exc:
        log.warning("designer failed for %s: %s", doc.path, exc)
        return None
    if proposal.target != "doc_kind" or not proposal.name or not proposal.description:
        return None
    registry.upsert_doc_kind(conn, proposal.name, proposal.description, origin="designer", status="proposed",
                             created_by=llm.label, recognise=proposal.recognise)
    return proposal.name
