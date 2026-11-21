"""Replay adapter: answers `extract` from the reference envelopes in structured/docs, matched by file hash.

It reads nothing. Its purpose is to run the whole live path (survey, extract, validation gate, facts, events, issues)
before a real provider exists, and to test that path. Facts it produces carry the extractor label `llm:replay`, so
nobody mistakes them for a real extraction. Files without a reference envelope raise an error.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

from tracker.config import PROJECT_ROOT

from .base import Document, LLMClient, Structure


class ReplayClient(LLMClient):
    name = "replay"
    needs_images = False

    def __init__(self, structured_dir: Path | None = None):
        self.docs = Path(structured_dir or os.environ.get("KB_STRUCTURED_DIR", PROJECT_ROOT / "structured")) / "docs"

    def extract(self, document: Document, structure: Structure, schema: dict, contract: dict) -> dict:
        path = self.docs / f"{document.sha256}.json"
        if not path.is_file():
            raise LookupError(f"no reference envelope for {document.path} ({document.sha256[:12]})")
        env = copy.deepcopy(json.loads(path.read_text()))
        env["doc_kind"] = env.get("doc_kind") or structure.name
        return env
