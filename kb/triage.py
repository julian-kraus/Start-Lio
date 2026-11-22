"""Tool-backed triage for documents that the normal KB vocabulary cannot classify.

Triage is deliberately conservative.  The role can inspect a document and nearby facts, but anything it writes is
labelled ``triage-agent`` and has its confidence capped until a person promotes or reviews it.  The tools are kept
here instead of in the model adapter so replay and live clients exercise the same database mutations.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable

from psycopg.types.json import Jsonb

from agents import llm
from agents.tools import ToolRegistry
from tracker.db import _jsonb_safe

from . import facts as facts_mod
from .engine import issue_key
from .llm.base import text_of
from .tabular import TABULAR_CONTAINERS

EXTRACTOR = "triage-agent"
DEFAULT_CONFIDENCE_CAP = 0.5
MAX_DOCUMENT_CHARS = 80_000
MAX_FACT_RESULTS = 50

_READ_SCHEMA = {
    "type": "object",
    "properties": {"path": {"type": "string"}},
    "required": ["path"],
}
_QUERY_SCHEMA = {
    "type": "object",
    "properties": {
        "fact_type": {"type": "string"},
        "attribute": {"type": "string"},
        "source_path": {"type": "string"},
        "subject": {"type": "object"},
        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_FACT_RESULTS},
    },
}
_ALIAS_SCHEMA = {
    "type": "object",
    "properties": {
        "alias_kind": {"type": "string"},
        "alias": {"type": "string"},
        "canonical_id": {"type": "string"},
        "source_path": {"type": "string"},
    },
    "required": ["alias_kind", "alias", "canonical_id"],
}
_FACT_SCHEMA = {
    "type": "object",
    "properties": {
        "fact_type": {"type": "string"},
        "subject": {"type": "object"},
        "attribute": {"type": "string"},
        "value": {},
        "unit": {"type": "string"},
        "evidence": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "source_path": {"type": "string"},
    },
    "required": ["fact_type", "subject", "attribute", "value", "evidence"],
}
_ISSUE_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string"},
        "severity": {"type": "string", "enum": ["high", "medium", "low", "info"]},
        "title": {"type": "string"},
        "payload": {"type": "object"},
        "fact_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["kind", "severity", "title"],
}


def _row_dict(row: Any) -> dict:
    return dict(row) if hasattr(row, "keys") else row


def _nonempty(value: Any, name: str) -> str:
    value = str(value or "").strip()
    if not value:
        raise ValueError(f"{name} must not be empty")
    return value


@dataclass
class TriageContext:
    conn: Any
    read_bytes: Callable[[str, str], bytes]
    confidence_cap: float = DEFAULT_CONFIDENCE_CAP
    current_path: str | None = None
    current_sha256: str | None = None

    def __post_init__(self) -> None:
        if not 0 <= self.confidence_cap <= 1:
            raise ValueError("confidence_cap must be between zero and one")

    def _path(self, path: str | None) -> str:
        return _nonempty(path or self.current_path, "path")

    def read_document(self, path: str) -> dict:
        path = self._path(path)
        row = self.conn.execute(
            "SELECT path, sha256, container, doc_kind, status FROM documents WHERE path = %s AND active "
            "ORDER BY updated_at DESC LIMIT 1", (path,)).fetchone()
        if row is None:
            raise LookupError(f"active document not found: {path}")
        row = _row_dict(row)
        data = self.read_bytes(row["sha256"], path)
        text, pages_without_text = text_of(row.get("container") or "text", data)
        if text is not None and len(text) > MAX_DOCUMENT_CHARS:
            text = text[:MAX_DOCUMENT_CHARS]
        return {**row, "text": text, "pages_without_text": pages_without_text,
                "truncated": text is not None and len(text) >= MAX_DOCUMENT_CHARS}

    def query_facts(self, *, fact_type: str | None = None, attribute: str | None = None,
                    source_path: str | None = None, subject: dict | None = None,
                    limit: int = MAX_FACT_RESULTS) -> dict:
        limit = max(1, min(int(limit), MAX_FACT_RESULTS))
        clauses = ["valid"]
        params: list[Any] = []
        for column, value in (("fact_type", fact_type), ("attribute", attribute), ("source_path", source_path)):
            if value:
                clauses.append(f"{column} = %s")
                params.append(value)
        if subject:
            clauses.append("subject @> %s")
            params.append(Jsonb(_jsonb_safe(subject)))
        params.append(limit)
        rows = self.conn.execute(
            "SELECT fact_id, fact_type, subject, attribute, value, unit, evidence, confidence, source_path, "
            "source_sha256, extractor FROM facts WHERE " + " AND ".join(clauses) +
            " ORDER BY observed_at DESC LIMIT %s", tuple(params)).fetchall()
        return {"facts": [_row_dict(r) for r in rows]}

    def propose_alias(self, *, alias_kind: str, alias: str, canonical_id: str,
                      source_path: str | None = None) -> dict:
        alias_kind, alias, canonical_id = (_nonempty(alias_kind, "alias_kind"), _nonempty(alias, "alias"),
                                           _nonempty(canonical_id, "canonical_id"))
        path = source_path or self.current_path
        source = EXTRACTOR if not path else f"{EXTRACTOR}:{path}"
        self.conn.execute(
            "INSERT INTO aliases (alias_kind, alias, canonical_id, source, confidence) VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (alias_kind, alias, canonical_id) DO UPDATE SET source = EXCLUDED.source, "
            "confidence = least(aliases.confidence, EXCLUDED.confidence)",
            (alias_kind, alias, canonical_id, source, self.confidence_cap))
        return {"proposed": True, "alias_kind": alias_kind, "alias": alias, "canonical_id": canonical_id,
                "confidence": self.confidence_cap}

    def emit_fact(self, *, fact_type: str, subject: dict, attribute: str, value: Any, evidence: str,
                  unit: str | None = None, confidence: float = 0.5, source_path: str | None = None) -> dict:
        path = self._path(source_path)
        sha = self.current_sha256
        if source_path and source_path != self.current_path:
            row = self.conn.execute("SELECT sha256 FROM documents WHERE path = %s AND active ORDER BY updated_at DESC LIMIT 1",
                                    (path,)).fetchone()
            sha = row["sha256"] if row else None
        sha = _nonempty(sha, "source_sha256")
        if not isinstance(subject, dict):
            raise TypeError("subject must be an object")
        fact = {"fact_type": _nonempty(fact_type, "fact_type"), "subject": subject,
                "attribute": _nonempty(attribute, "attribute"), "value": value, "unit": unit,
                "evidence": _nonempty(evidence, "evidence"), "extractor": EXTRACTOR,
                "confidence": max(0.0, min(float(confidence), self.confidence_cap)), "source_path": path,
                "source_sha256": sha}
        fact["fact_id"] = facts_mod.fact_id(fact)
        facts_mod.insert_facts(self.conn, [fact])
        return {"emitted": True, "fact_id": fact["fact_id"], "confidence": fact["confidence"],
                "extractor": EXTRACTOR}

    def open_issue(self, *, kind: str, severity: str, title: str, payload: dict | None = None,
                   fact_ids: list[str] | None = None) -> dict:
        kind, severity, title = (_nonempty(kind, "kind"), _nonempty(severity, "severity"), _nonempty(title, "title"))
        if severity not in {"high", "medium", "low", "info"}:
            raise ValueError("severity must be high, medium, low, or info")
        body = dict(payload or {})
        if self.current_path:
            body.setdefault("source_path", self.current_path)
        key = issue_key(kind, self.current_path or "triage", title)
        payload_hash = hashlib.sha256(json.dumps([severity, title, body], sort_keys=True, default=str).encode()).hexdigest()
        self.conn.execute(
            "INSERT INTO issues (issue_key, kind, severity, state, title, payload, payload_hash, fact_ids) "
            "VALUES (%s, %s, %s, 'open', %s, %s, %s, %s) ON CONFLICT (issue_key) DO UPDATE SET state = 'open', "
            "severity = EXCLUDED.severity, title = EXCLUDED.title, payload = EXCLUDED.payload, "
            "payload_hash = EXCLUDED.payload_hash, fact_ids = EXCLUDED.fact_ids, last_changed = now(), resolved_at = NULL",
            (key, kind, severity, title, Jsonb(_jsonb_safe(body)), payload_hash, list(fact_ids or [])))
        return {"opened": True, "issue_key": key, "kind": kind, "severity": severity}


def make_registry(context: TriageContext) -> ToolRegistry:
    """Build the fixed tool set for one document, with writes bound to that document's context."""
    registry = ToolRegistry()
    registry.add("read_document", "Read the active unclassified document text and metadata.", _READ_SCHEMA,
                 context.read_document)
    registry.add("query_facts", "Find existing valid facts to ground an alias or new fact.", _QUERY_SCHEMA,
                 context.query_facts)
    registry.add("propose_alias", "Record an alias proposal with triage confidence.", _ALIAS_SCHEMA,
                 context.propose_alias)
    registry.add("emit_fact", "Emit one reviewed-looking fact with a capped triage confidence.", _FACT_SCHEMA,
                 context.emit_fact)
    registry.add("open_issue", "Open a review issue when evidence is insufficient or ambiguous.", _ISSUE_SCHEMA,
                 context.open_issue)
    return registry


def candidate_documents(conn, limit: int = 20) -> list[dict]:
    """Return active documents with no settled kind, including documents explicitly classified as ``other``."""
    limit = max(1, min(int(limit), 100))
    rows = conn.execute(
        "SELECT path, sha256, container, doc_kind, status FROM documents WHERE active "
        "AND (doc_kind = 'other' OR (doc_kind IS NULL AND status = 'queued')) "
        "AND (container IS NULL OR NOT (container = ANY(%s))) ORDER BY updated_at, path LIMIT %s",
        (list(TABULAR_CONTAINERS), limit)).fetchall()
    return [_row_dict(row) for row in rows]


def run_one(client: llm.LLMClient, context: TriageContext, *, tracer=None, max_turns: int = 12) -> dict:
    """Run triage for the document bound to ``context`` and return a serialisable result."""
    if not context.current_path or not context.current_sha256:
        raise ValueError("run_one requires current_path and current_sha256")
    prompt = (f"Triage document {context.current_path} ({context.current_sha256}). Read it first. "
              "Use query_facts to check identifiers and aliases before writing. Emit only facts supported by "
              "document evidence, propose aliases when a known canonical id is clear, and open an issue for "
              "ambiguity or missing evidence. Do not treat a proposal as settled knowledge.")
    result = llm.run(
        client, make_registry(context), role="triage", tracer=tracer, max_turns=max_turns,
        system=("You triage an unclassified business document. You must use the supplied tools for every read and "
                "write. Keep all emitted facts conservative: the tools cap proposal confidence. Return JSON with "
                "action (completed, needs_review, or no_action) and a short summary."),
        prompt=prompt,
        schema={"type": "object", "properties": {"action": {"type": "string"}, "summary": {"type": "string"}},
                "required": ["action", "summary"]})
    return {"path": context.current_path, "ok": result.ok, "action": (result.verdict or {}).get("action"),
            "summary": (result.verdict or {}).get("summary"), "calls": result.calls,
            "error": result.error, "guard_failed": result.guard_failed}


def run(conn, client: llm.LLMClient, read_bytes: Callable[[str, str], bytes], *, limit: int = 20,
        confidence_cap: float = DEFAULT_CONFIDENCE_CAP, tracer=None, max_turns: int = 12) -> dict:
    """Triage pending unclassified documents using a replay or live agent client."""
    results = []
    for item in candidate_documents(conn, limit):
        context = TriageContext(conn, read_bytes, confidence_cap, item["path"], item["sha256"])
        results.append(run_one(client, context, tracer=tracer, max_turns=max_turns))
    return {"processed": len(results), "results": results}


triage = run
