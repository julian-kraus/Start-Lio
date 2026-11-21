"""The extraction interface every LLM provider adapter implements, plus the queue worker that uses it.

An adapter is configured with KB_LLM_ADAPTER="package.module:ClassName". Until one exists, documents wait in
kb.extraction_queue and show up as `pending_extraction` issues; nothing is skipped silently.
"""

from __future__ import annotations

import importlib
import io
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from pypdf import PdfReader

from tracker.diff import _docx_text

from .. import facts as facts_mod
from ..loader import load_envelope


@dataclass
class Document:
    """A source file prepared so that any vision-capable model can read it."""
    path: str
    sha256: str
    container: str
    text: str | None = None                       # text layer or plain text, if any
    images: list[tuple[str, bytes]] = field(default_factory=list)   # (media type, bytes) per page or image
    pages_without_text: list[int] = field(default_factory=list)


class LLMClient(ABC):
    """One method: read a document and return an envelope dict that follows the contract schema."""
    name: str = "llm"

    @abstractmethod
    def extract(self, document: Document, schema: dict, instructions: str) -> dict:
        ...


def render_pdf_pages(data: bytes) -> list[bytes]:
    """PNG per page. Needs pypdfium2, which an adapter that sends images must install."""
    try:
        import pypdfium2
    except ImportError as exc:
        raise RuntimeError("rendering PDF pages needs the pypdfium2 package") from exc
    pdf = pypdfium2.PdfDocument(data)
    out = []
    for page in pdf:
        buf = io.BytesIO()
        page.render(scale=2).to_pil().save(buf, format="PNG")
        out.append(buf.getvalue())
    return out


def normalise(path: str, sha: str, container: str, data: bytes, *, render: bool = True) -> Document:
    doc = Document(path, sha, container)
    if container == "pdf":
        reader = PdfReader(io.BytesIO(data))
        texts = [(page.extract_text() or "") for page in reader.pages]
        doc.text = "\n\f\n".join(texts)
        doc.pages_without_text = [i + 1 for i, t in enumerate(texts) if len(t.strip()) < 20]
        if render:
            doc.images = [("image/png", png) for png in render_pdf_pages(data)]
    elif container == "image":
        media = "image/png" if data.startswith(b"\x89PNG") else "image/jpeg"
        doc.images = [(media, data)]
    elif container == "docx":
        doc.text = _docx_text(data)
    else:
        doc.text = data.decode("utf-8", errors="replace")
    return doc


def load_adapter(spec: str | None) -> LLMClient | None:
    if not spec:
        return None
    module_name, _, class_name = spec.partition(":")
    if not class_name:
        raise SystemExit(f"KB_LLM_ADAPTER must look like package.module:ClassName, got {spec!r}")
    cls = getattr(importlib.import_module(module_name), class_name)
    client = cls()
    if not isinstance(client, LLMClient):
        raise SystemExit(f"{spec} does not implement kb.llm.base.LLMClient")
    return client


def instructions_for(conn, structured: Path) -> str:
    """The doc-kind registry and the learnings are the instructions; nothing about the data lives in code."""
    parts = ["Classify the document against these document kinds and extract facts as the matching kind describes. "
             "If no kind fits, propose a new kind in doc_kind and use x_<name> fact types."]
    for row in conn.execute("SELECT name, description FROM doc_kinds ORDER BY name"):
        parts.append(f"## Document kind: {row['name']}\n{row['description']}")
    learnings = structured / "LEARNINGS.md"
    if learnings.is_file():
        parts.append("## Learnings from earlier extraction\n" + learnings.read_text())
    return "\n\n".join(parts)


def drain_queue(conn, client: LLMClient, read_bytes, structured: Path, confidence_map: dict,
                limit: int = 20) -> dict:
    schema_path = structured / "contract" / "envelope.schema.json"
    if not schema_path.is_file():
        raise SystemExit(f"{schema_path} is missing; the contract must exist before LLM extraction")
    schema = json.loads(schema_path.read_text())
    instructions = instructions_for(conn, structured)
    counts = {"done": 0, "failed": 0}
    items = conn.execute("SELECT path, sha256, container FROM extraction_queue WHERE state = 'pending' "
                         "ORDER BY enqueued_at LIMIT %s", (limit,)).fetchall()
    for item in items:
        try:
            doc = normalise(item["path"], item["sha256"], item["container"], read_bytes(item["sha256"], item["path"]))
            env = client.extract(doc, schema, instructions)
            env["source"] = {"path": item["path"], "sha256": item["sha256"]}
            env["extracted_by"] = f"llm-{client.name}"
            load_envelope(conn, env, current_sha=item["sha256"], confidence_map=confidence_map)
            facts_mod.upsert_document(conn, item["path"], item["sha256"], container=item["container"],
                                      status="loaded", extractor=f"llm:{client.name}", doc_kind=env.get("doc_kind"))
            conn.execute("UPDATE extraction_queue SET state = 'done', updated_at = now() WHERE path = %s AND sha256 = %s",
                         (item["path"], item["sha256"]))
            counts["done"] += 1
        except Exception as exc:  # one bad document must not stop the queue
            conn.rollback()
            conn.execute(
                "UPDATE extraction_queue SET attempts = attempts + 1, last_error = %s, updated_at = now(), "
                "state = CASE WHEN attempts + 1 >= 3 THEN 'failed' ELSE state END WHERE path = %s AND sha256 = %s",
                (f"{type(exc).__name__}: {exc}"[:2000], item["path"], item["sha256"]))
            counts["failed"] += 1
        conn.commit()
    return counts
