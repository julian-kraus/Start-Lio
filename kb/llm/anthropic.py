"""Anthropic provider adapter for the three ingestion roles (classify, extract, design).

It reads the key from LOOKAHEAD_ANTHROPIC_API_KEY and the model from LOOKAHEAD_LLM_MODEL, sends the document text
and, for scans, page images, and asks for a strict-JSON answer that the pipeline already knows how to use.

Two findings from the extraction probe are built in:
- quantities are returned verbatim and parsed in code (space-grouped numbers such as "10 000" were misread),
  so `extract` parses only attributes the contract declares `type: number`, leaving identifiers and dates text;
- every answer is cached per sha256 and per role, so a document is paid for once. Tests replay recorded answers
  from the same on-disk store without a key: a cache hit and a replay fixture are the same file.

Select it with KB_LLM_ADAPTER="kb.llm.anthropic:AnthropicClient".
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from tracker.config import PROJECT_ROOT, load_env

from .anthropic_parse import (_ENVELOPE_KEYS, _QUANTITY_ATTR, SPEC_VERSION, _check_shape, _coerce_number,  # noqa: F401
                              _declared_subject_keys, _envelope, _numeric_attrs, _parse_json)
from .base import Document, LLMClient, Routing, Structure, StructureProposal


_MAX_TOKENS_RETRY_SCALE = 4     # one retry at a larger output budget when a long document hits the output cap


class ExtractionStop(RuntimeError):
    """A hard stop from the provider (max_tokens, refusal, timeout): the document cannot be read this time."""


@dataclass
class Reply:
    stop_reason: str
    text: str
    usage: dict


def _bool_env(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    return default if value is None else value.strip().lower() in ("1", "true", "yes", "on")


class ApiTransport:
    """The live path: the official `anthropic` SDK, imported only when a cache miss actually needs the network."""

    def __init__(self, api_key: str, timeout: float, max_retries: int = 0):
        import anthropic

        self._errors = anthropic
        # The pipeline already retries a failed document (intake.drain), so the SDK must not add its own two
        # retries on top: a timing-out document otherwise costs three times the requests.
        self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=max_retries)

    def create(self, request: dict) -> Reply:
        try:
            msg = self._client.messages.create(**request)
        except self._errors.APITimeoutError as exc:
            raise ExtractionStop(f"timeout: {exc}") from exc
        text = "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", None) == "text")
        usage = {"input_tokens": getattr(msg.usage, "input_tokens", None),
                 "output_tokens": getattr(msg.usage, "output_tokens", None)} if msg.usage else {}
        return Reply(msg.stop_reason or "end_turn", text, usage)


def _store_path(store_dir: Path, role: str, model: str, sha: str, aux: str = "") -> Path:
    model_slug = re.sub(r"[^A-Za-z0-9_.-]", "-", model or "model")
    tail = f".{aux}" if aux else ""
    return store_dir / f"{role}.{model_slug}.{sha}{tail}.json"


def _aux_key(*parts) -> str:
    """A short, stable hash of the inputs that change an answer besides the document bytes (kind list and path
    for classify, structure and contract vocabulary for extract, sample rows and path for design). It joins the
    document sha in the cache key so two sheets of one workbook, a classify after a new kind, or two paths with
    one sha do not collide on the sha alone and replay a stale answer."""
    blob = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


# A document is paid for once, so the cache is on by default. It lives in a git-ignored directory so answers
# are never committed; set KB_LLM_CACHE_DIR to move it.
DEFAULT_CACHE_DIR = PROJECT_ROOT / ".cache" / "kb_llm"


def _default_cache_dir() -> Path:
    """Where the cache lives when KB_LLM_CACHE_DIR is unset. In the container /app is root-owned, so /app/.cache
    cannot be created; TRACKER_DATA_DIR (the writable volume) is used there. Locally, .cache/kb_llm under root."""
    base = os.environ.get("TRACKER_DATA_DIR")
    return Path(base) / "kb_llm_cache" if base else DEFAULT_CACHE_DIR


class AnthropicClient(LLMClient):
    name = "anthropic"
    needs_images = True

    def __init__(self, *, api_key: str | None = None, model: str | None = None, store_dir: str | Path | None = None,
                 transport: ApiTransport | None = None, record: bool | None = None, timeout: float | None = None,
                 max_pages: int | None = None, max_output_tokens: int | None = None,
                 max_input_chars: int | None = None):
        load_env(PROJECT_ROOT / "secrets" / "anthropic.env")
        env = os.environ
        self.model = model or env.get("LOOKAHEAD_LLM_MODEL") or "claude-haiku-5-5"
        self._api_key = api_key or env.get("LOOKAHEAD_ANTHROPIC_API_KEY")
        self._timeout = timeout if timeout is not None else float(env.get("KB_LLM_TIMEOUT", "120"))
        self.max_pages = max_pages if max_pages is not None else int(env.get("KB_LLM_MAX_PAGES", "20"))
        self.max_output_tokens = (max_output_tokens if max_output_tokens is not None
                                  else int(env.get("KB_LLM_MAX_OUTPUT_TOKENS", "4096")))
        self.max_input_chars = (max_input_chars if max_input_chars is not None
                                else int(env.get("KB_LLM_MAX_INPUT_CHARS", "120_000")))
        self.record = _bool_env("KB_LLM_RECORD", True) if record is None else record
        store = store_dir if store_dir is not None else env.get("KB_LLM_CACHE_DIR") or _default_cache_dir()
        self.store_dir = Path(store)
        self._transport = transport

    # ---- transport and cache -------------------------------------------------

    def _transport_or_fail(self) -> ApiTransport:
        if self._transport is None:
            if not self._api_key:
                raise ExtractionStop("no LOOKAHEAD_ANTHROPIC_API_KEY set and no recorded reply to fall back on")
            self._transport = ApiTransport(self._api_key, self._timeout)
        return self._transport

    def _answer(self, role: str, sha: str, system: str, content: list[dict], aux: str = "") -> dict:
        request = {"model": self.model, "max_tokens": self.max_output_tokens, "system": system,
                   "messages": [{"role": "user", "content": content}]}
        # The cache key covers the whole request sent (system prompt, reading rules, page and text caps, output
        # budget), so a changed prompt or cap never replays an answer made from a different request.
        key = _aux_key(aux, hashlib.sha256(json.dumps(request, sort_keys=True).encode("utf-8")).hexdigest())
        cached = _store_path(self.store_dir, role, self.model, sha, key) if self.store_dir else None
        if cached and cached.is_file():
            try:
                d = json.loads(cached.read_text())
                data = _parse_json(d.get("text", ""))
                _check_shape(role, data)
                return data
            except (ValueError, json.JSONDecodeError):
                pass            # a malformed cached answer is ignored, never served; fall through to a live call
        reply = self._transport_or_fail().create(request)
        if reply.stop_reason == "max_tokens":
            # A long document (a dense scan of line items) can overrun the output cap. Retry once with a larger
            # budget before giving up; a second max_tokens is a real hard stop, not a budget that was too small.
            request["max_tokens"] = self.max_output_tokens * _MAX_TOKENS_RETRY_SCALE
            reply = self._transport_or_fail().create(request)
        # Only end_turn is a finished answer. max_tokens, refusal and model_context_window_exceeded all leave a
        # truncated or missing answer: treat every other stop as a hard stop for the document.
        if reply.stop_reason != "end_turn":
            raise ExtractionStop(f"{reply.stop_reason}: the model did not finish a usable answer")
        data = _parse_json(reply.text)        # parse and shape-check before caching: a bad reply is never stored
        _check_shape(role, data)
        if cached and self.record:
            try:
                cached.parent.mkdir(parents=True, exist_ok=True)
                cached.write_text(json.dumps({"stop_reason": reply.stop_reason, "text": reply.text,
                                              "usage": reply.usage}, ensure_ascii=False, indent=2))
            except OSError:
                pass            # an unwritable cache (read-only volume) must not discard a paid-for answer
        return data

    # ---- prompt building -----------------------------------------------------
    def _content(self, document: Document, instruction: str) -> tuple[list[dict], list[str]]:
        """Build the prompt blocks and a list of notes for anything cut, so a caller records what did not reach
        the model instead of a clean read. Both the text cap and the page-image cap are enforced here."""
        blocks: list[dict] = [{"type": "text", "text": instruction}]
        notes: list[str] = []
        if document.text and document.text.strip():
            text = document.text
            if len(text) > self.max_input_chars:
                notes.append(f"document text was truncated to {self.max_input_chars} of {len(text)} characters")
                text = text[:self.max_input_chars]
            blocks.append({"type": "text", "text": "Document text:\n" + text})
        sparse = not document.text or len(document.text.strip()) < 200 or bool(document.pages_without_text)
        if sparse and document.images:
            # Pages with no text layer can only be read as an image, so send those first within the page cap.
            no_text = {p for p in document.pages_without_text if 1 <= p <= len(document.images)}
            order = [i for i in range(len(document.images)) if (i + 1) in no_text]
            order += [i for i in range(len(document.images)) if (i + 1) not in no_text]
            for i in order[:self.max_pages]:
                media_type, data = document.images[i]
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": media_type,
                                                           "data": base64.b64encode(data).decode("ascii")}})
            dropped = order[self.max_pages:]
            if dropped:
                notes.append(f"{len(dropped)} of {len(document.images)} page images exceeded the cap of "
                             f"{self.max_pages} and were not sent; pages without a text layer were sent first")
                for i in sorted(p for p in dropped if (p + 1) in no_text):
                    notes.append(f"page {i + 1}: scanned page with no text layer was not sent (image cap "
                                 f"{self.max_pages} reached)")
        return blocks, notes

    def classify(self, document: Document, kinds: list[Structure]) -> Routing:
        catalogue = [{"name": k.name, "description": (k.description or "")[:600]} for k in kinds]
        system = (
            "You route one supplier document to a known kind. You are given the kinds and the document. "
            "Answer with one JSON object and nothing else: {\"decision\": \"kind|none|ambiguous\", "
            "\"kind\": <name or null>, \"reason\": <short string>}. Use \"kind\" only when one kind clearly fits, "
            "\"ambiguous\" when two or more fit, \"none\" when nothing fits.")
        instruction = "Known kinds:\n" + json.dumps(catalogue, ensure_ascii=False) + f"\n\nFile path: {document.path}"
        content, _notes = self._content(document, instruction)
        # The path is in the prompt, so it joins the cache key: two paths with one sha route independently (#67).
        aux = _aux_key("classify", catalogue, document.path)
        data = self._answer("classify", document.sha256, system, content, aux=aux)
        decision = data.get("decision") if data.get("decision") in ("kind", "none", "ambiguous") else "none"
        kind = data.get("kind") if decision == "kind" else None
        return Routing(decision, kind, str(data.get("reason") or "")[:500], self.label)

    def extract(self, document: Document, structure: Structure, schema: dict, contract: dict) -> dict:
        declared = _declared_subject_keys()
        vocabulary = [{"name": t.get("name"), "attributes": t.get("attributes"),
                       "subject_keys": t.get("subject_keys") or declared.get(t.get("name")) or []}
                      for t in (contract.get("fact_types") or [])]
        system = (
            "You read one supplier document into facts that follow a fixed contract. Answer with one JSON object "
            "and nothing else, with exactly these keys: doc_kind, doc_kind_confidence (high|medium|low), languages "
            "(array of ISO codes), doc_date (YYYY-MM-DD or null), facts (array), unreadable (array of strings), "
            "proposed_fact_types (array). Each fact is {fact_type, subject (object of the values that identify the "
            "record), attribute, value, unit, locator ({page, section}), evidence (the exact text you read the "
            "value from), confidence (high|medium|low)}. For each contract fact type use exactly the subject keys "
            "it lists as the subject object's keys (never invented names) and use only the attribute names it "
            "lists for that type. An identifier the document does not print but its source path names (for "
            "example the folder of the sender) may be taken from the path. A subject key named line, ending in _line, or statement_no is the 1-based "
            "position of the record in the document, counted from the top: 1 for the first line, never a part, "
            "material or item number. For every record you report, give one fact for each attribute its fact type "
            "lists. The value is the text you read, the same text you quote as evidence; use null only when the "
            "document shows that attribute empty or unmarked (false for a yes/no attribute such as "
            "struck_through), never for a value you can quote. A handwritten or "
            "corrected quantity goes in its own attribute, and the quantity that finally counts in final_qty. Copy every quantity verbatim as it "
            "appears in the document, keeping any digit grouping (for example \"10 000\"); do not round or reformat "
            "it. Use only fact types from the contract; for genuinely new information name the type x_<something> "
            "and list it in proposed_fact_types.")
        instruction = (f"Document kind: {structure.name}\nSource path: {document.path}\n"
                       f"Reading rules:\n{(structure.description or '')[:4000]}\n\n"
                       f"Contract fact types:\n{json.dumps(vocabulary, ensure_ascii=False)}")
        content, notes = self._content(document, instruction)
        aux = _aux_key("extract", structure.name, vocabulary, document.path)
        data = self._answer("extract", document.sha256, system, content, aux=aux)
        attrs = {v["name"]: set(v.get("attributes") or {}) for v in vocabulary if v.get("name")}
        env = _envelope(data, structure.name, _numeric_attrs(contract), attrs)
        if notes:                       # pages or text the cap cut never reach the model: record, do not hide
            env["unreadable"] = list(env["unreadable"]) + notes
        return env

    def design(self, document: Document, similar: list[Structure], contract: dict,
               sample_rows: list[list[str]] | None = None) -> StructureProposal:
        known = [{"name": k.name, "description": (k.description or "")[:300]} for k in similar]
        system = (
            "You propose a structure for a document or table the registry does not recognise yet. Answer with one "
            "JSON object and nothing else. For a document: {\"target\": \"doc_kind\", \"name\": <snake_case>, "
            "\"reason\": <string>, \"description\": <markdown reading rules>, \"recognise\": {\"path_globs\": [...], "
            "\"any_phrases\": [...], \"container\": <string>}}. For a table: {\"target\": \"schema_map\", \"name\": "
            "<string>, \"reason\": <string>, \"schema_map\": {\"signature\": <string>, \"format\": <string>, "
            "\"fact_type\": <string>, \"subject\": [<entity id columns>], \"columns\": {<header>: {\"field\": ..., "
            "\"type\": ...}}}}. Subjects are entity ids, never row or line numbers.")
        instruction = ("Nearest known kinds:\n" + json.dumps(known, ensure_ascii=False) +
                       f"\n\nFile path: {document.path}")
        rows = sample_rows[:30] if sample_rows else None
        if rows:
            instruction += "\n\nSample rows:\n" + json.dumps(rows, ensure_ascii=False)
        content, _notes = self._content(document, instruction)
        # The sample rows and the path are in the prompt, so both join the cache key: two sheets of one workbook
        # (same sha, different rows) and two paths with one sha each get their own answer, not the first one.
        data = self._answer("design", document.sha256, system, content, aux=_aux_key("design", known, rows, document.path))
        target = data.get("target")
        if target not in ("doc_kind", "schema_map"):
            target = "schema_map" if data.get("schema_map") else "doc_kind"
        return StructureProposal(target=target, name=str(data.get("name") or "").strip(),
                                 reason=str(data.get("reason") or "")[:500], description=data.get("description"),
                                 recognise=data.get("recognise"), schema_map=data.get("schema_map"))
