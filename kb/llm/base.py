"""The interface every LLM provider adapter implements. No provider is wired in yet.

Three roles, matching the ingestion flow:
- classify: survey a file the cheap checks could not route (known doc kind, nothing fits, ambiguous);
- extract:  read one document into an envelope that follows the contract (structuring agent);
- design:   propose a new structure (doc kind or table schema map) for something unknown (structure designer).

An adapter may implement a subset; `supports()` tells the pipeline which roles are available. Adapters are selected
with KB_LLM_ADAPTER="package.module:ClassName".
"""

from __future__ import annotations

import importlib
import io
from abc import ABC
from dataclasses import dataclass, field

from pypdf import PdfReader

from tracker.diff import _docx_text

ROLES = ("classify", "extract", "design")


@dataclass
class Document:
    """A source file prepared so that any model can read it: text layer and/or page images."""
    path: str
    sha256: str
    container: str
    text: str | None = None
    images: list[tuple[str, bytes]] = field(default_factory=list)   # (media type, bytes) per page or image
    pages_without_text: list[int] = field(default_factory=list)


@dataclass
class Structure:
    """A doc kind from the registry: the 'structure file' the structuring agent follows."""
    name: str
    description: str
    recognise: dict | None = None
    status: str = "core"


@dataclass
class Routing:
    decision: str            # kind | none | ambiguous
    kind: str | None
    reason: str
    by: str                  # rules | llm:<adapter> | reuse


@dataclass
class StructureProposal:
    target: str              # doc_kind | schema_map
    name: str
    reason: str
    description: str | None = None        # doc_kind: markdown with reading rules
    recognise: dict | None = None         # doc_kind: path_globs / any_phrases / container
    schema_map: dict | None = None        # schema_map: same format as structured/schema_maps


class LLMClient(ABC):
    name: str = "llm"
    model: str | None = None
    needs_images: bool = True    # False lets the pipeline skip rendering PDF pages

    def classify(self, document: Document, kinds: list[Structure]) -> Routing:
        raise NotImplementedError

    def extract(self, document: Document, structure: Structure, schema: dict, contract: dict) -> dict:
        raise NotImplementedError

    def design(self, document: Document, similar: list[Structure], contract: dict,
               sample_rows: list[list[str]] | None = None) -> StructureProposal:
        raise NotImplementedError

    def supports(self, role: str) -> bool:
        return getattr(type(self), role) is not getattr(LLMClient, role)

    @property
    def label(self) -> str:
        return f"llm:{self.name}" + (f"/{self.model}" if self.model else "")


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


def text_of(container: str, data: bytes) -> tuple[str | None, list[int]]:
    """Text layer (PDF), document text (docx) or plain text; images have none."""
    if container == "pdf":
        reader = PdfReader(io.BytesIO(data))
        texts = [(page.extract_text() or "") for page in reader.pages]
        return "\n\f\n".join(texts), [i + 1 for i, t in enumerate(texts) if len(t.strip()) < 20]
    if container == "docx":
        return _docx_text(data), []
    if container in ("image", "binary", "zip"):
        return None, []
    return data.decode("utf-8", errors="replace"), []


def normalise(path: str, sha: str, container: str, data: bytes, *, render: bool = True) -> Document:
    text, empty_pages = text_of(container, data)
    doc = Document(path, sha, container, text=text, pages_without_text=empty_pages)
    if container == "pdf" and render:
        doc.images = [("image/png", png) for png in render_pdf_pages(data)]
    elif container == "image":
        doc.images = [("image/png" if data.startswith(b"\x89PNG") else "image/jpeg", data)]
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
