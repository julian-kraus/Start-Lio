"""Live intake: drain the extraction queue through the configured LLM adapter.

Per document: kind from the survey (rules) -> else the LLM survey (classify) -> else the designer proposes a kind ->
the structuring agent (extract) reads it -> the validation gate decides whether its facts count.
A document that fits no kind stays pending, and so a visible issue, but is tried again only when the doc kinds or the
adapter change.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from psycopg.types.json import Jsonb

from tracker.db import _jsonb_safe

from . import designer, registry, validate
from . import facts as facts_mod
from .llm.base import LLMClient, Structure, normalise
from .loader import load_envelope

log = logging.getLogger(__name__)
NO_FIT = "no_fit"   # in a queue item's routing: what it was tried against when no kind fitted


def _tried_against(client: LLMClient, kinds: list[Structure]) -> str:
    """Changes when another answer is possible. A promotion changes only the status, so it is left out."""
    offer = [client.label, [[k.name, k.description, k.recognise] for k in kinds]]
    return hashlib.sha256(json.dumps(offer, sort_keys=True, default=str).encode()).hexdigest()[:16]


class _DesignCall:
    """designer.propose_doc_kind turns an error into "no proposal". This keeps the error, so that an outage is a
    failed attempt and is not recorded as a document that fits no kind."""

    def __init__(self, client: LLMClient):
        self.client, self.label = client, client.label
        self.error: Exception | None = None

    def design(self, *args, **kwargs):
        try:
            return self.client.design(*args, **kwargs)
        except Exception as exc:
            self.error = exc
            raise


def _set_queue(conn, item: dict, **fields) -> None:
    sets = ", ".join(f"{k} = %s" for k in fields) + ", updated_at = now()"
    values = [Jsonb(_jsonb_safe(v)) if isinstance(v, dict) else v for v in fields.values()]
    conn.execute(f"UPDATE extraction_queue SET {sets} WHERE path = %s AND sha256 = %s",
                 (*values, item["path"], item["sha256"]))


def drain(conn, client: LLMClient, read_bytes, structured: Path, confidence_map: dict, *, limit: int = 20,
          max_attempts: int = 3) -> dict:
    counts = {"done": 0, "needs_review": 0, "unrouted": 0, "failed": 0}
    if not client.supports("extract"):
        raise SystemExit(f"adapter {client.label} cannot extract")
    schema = validate.load_schema(structured)
    # Documents never tried go first, so second tries after a registry change cannot hold up the queue either.
    items = conn.execute(
        "SELECT path, sha256, container, doc_kind, routing, attempts FROM extraction_queue "
        "WHERE state = 'pending' AND routing->>%s IS DISTINCT FROM %s "
        "ORDER BY routing->>%s IS NOT NULL, enqueued_at LIMIT %s",
        (NO_FIT, _tried_against(client, registry.structures(conn)), NO_FIT, limit)).fetchall()
    for item in items:
        try:
            outcome = _one(conn, client, item, read_bytes, structured, schema, confidence_map)
            counts[outcome] += 1
        except Exception as exc:  # one bad document must not stop the queue
            conn.rollback()
            attempts = item["attempts"] + 1
            _set_queue(conn, item, attempts=attempts, last_error=f"{type(exc).__name__}: {exc}"[:2000],
                       state="failed" if attempts >= max_attempts else "pending")
            counts["failed"] += 1
            log.warning("intake failed for %s: %s", item["path"], exc)
        conn.commit()
    return counts


def _one(conn, client: LLMClient, item: dict, read_bytes, structured: Path, schema, confidence_map) -> str:
    data = read_bytes(item["sha256"], item["path"])
    doc = normalise(item["path"], item["sha256"], item["container"], data, render=client.needs_images)
    kind_name = item["doc_kind"]
    routing = {k: v for k, v in (item["routing"] or {}).items() if k != NO_FIT}
    kinds = registry.structures(conn)
    if kind_name is None and client.supports("classify"):
        r = client.classify(doc, kinds)
        routing = {**vars(r), "by": client.label, "rules": routing.get("rules", routing)}
        kind_name = r.kind if r.decision == "kind" else None
    if kind_name is None and client.supports("design"):
        call = _DesignCall(client)
        kind_name = designer.propose_doc_kind(conn, call, doc)
        if call.error:
            raise call.error
        if kind_name:
            routing = {**routing, "designed_kind": kind_name}
    structure = registry.structure(conn, kind_name) if kind_name else None
    if structure is None:
        _set_queue(conn, item, routing={**routing, NO_FIT: _tried_against(client, kinds)})
        return "unrouted"
    env = client.extract(doc, structure, schema or {}, registry.contract_summary(conn))
    env["source"] = {**(env.get("source") or {}), "path": item["path"], "sha256": item["sha256"],
                     "bytes": len(data)}
    env["extracted_by"] = client.label
    env["doc_kind"] = env.get("doc_kind") or structure.name
    kind_checks = (structure.recognise or {}).get("checks")
    gate = validate.check(env, schema=schema, known_types=registry.known_fact_types(conn), text=doc.text,
                          kind_checks=kind_checks)
    env = validate.apply(env, gate)
    load_envelope(conn, env, current_sha=item["sha256"] if gate.ok else None, confidence_map=confidence_map)
    status = "loaded" if gate.ok else "needs_review"
    facts_mod.upsert_document(conn, item["path"], item["sha256"], container=item["container"], status=status,
                              extractor=client.label, doc_kind=env["doc_kind"],
                              detail={"routing": routing, "gate": env["gate"]})
    _set_queue(conn, item, state="done" if gate.ok else "review", routing=routing, doc_kind=structure.name)
    return "done" if gate.ok else "needs_review"
