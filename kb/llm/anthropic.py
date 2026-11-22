"""Anthropic provider adapter for the three ingestion roles (classify, extract, design).

It reads the key from LOOKAHEAD_ANTHROPIC_API_KEY and the model from LOOKAHEAD_LLM_MODEL, sends the document text
and, for scans, page images, and asks for a strict-JSON answer that the pipeline already knows how to use.

Two findings from the extraction probe are built in:
- quantities are returned verbatim as text and parsed in code (space-grouped numbers such as "10 000" were
  misread by smaller models), so `extract` parses the value only for attributes the contract declares
  `type: number`, after the model has answered, and leaves identifiers, dates and codes as text;
- every answer is cached per sha256 and per role, so a document is read (and paid for) once. The same on-disk
  store is how tests replay recorded answers without a key: a cache hit and a replay fixture are the same file.

Select it with KB_LLM_ADAPTER="kb.llm.anthropic:AnthropicClient".
"""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from tracker.config import PROJECT_ROOT, load_env

from .base import Document, LLMClient, Routing, Structure, StructureProposal

SPEC_VERSION = "anthropic-extract-v1"
_WS = re.compile(r"[\s   ]")
_INT = re.compile(r"[+-]?\d+")
_DEC = re.compile(r"[+-]?\d+\.\d+")
# Two or more same-separator groups of three ("1.250.000", "1,250,000") cannot be a decimal, so it is
# safe to read them as one integer. A single group ("10.000", "1,250") is ambiguous with a decimal and is
# left as text for review, the same caution the probe finding asks for with space-grouped numbers.
_GROUPED = re.compile(r"[+-]?\d{1,3}([.,])\d{3}(?:\1\d{3})+")
_ENVELOPE_KEYS = ("source", "doc_kind", "doc_kind_confidence", "languages", "spec_version", "extracted_by",
                  "doc_date", "facts", "unreadable", "proposed_fact_types")


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


def _store_path(store_dir: Path, role: str, model: str, sha: str) -> Path:
    model_slug = re.sub(r"[^A-Za-z0-9_.-]", "-", model or "model")
    return store_dir / f"{role}.{model_slug}.{sha}.json"


# A document is paid for once, so the cache is on by default. It lives in a git-ignored directory so answers
# are never committed; set KB_LLM_CACHE_DIR to move it.
DEFAULT_CACHE_DIR = PROJECT_ROOT / ".cache" / "kb_llm"


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
        store = store_dir if store_dir is not None else env.get("KB_LLM_CACHE_DIR") or DEFAULT_CACHE_DIR
        self.store_dir = Path(store)
        self._transport = transport

    # ---- transport and cache -------------------------------------------------

    def _transport_or_fail(self) -> ApiTransport:
        if self._transport is None:
            if not self._api_key:
                raise ExtractionStop("no LOOKAHEAD_ANTHROPIC_API_KEY set and no recorded reply to fall back on")
            self._transport = ApiTransport(self._api_key, self._timeout)
        return self._transport

    def _answer(self, role: str, sha: str, system: str, content: list[dict]) -> dict:
        cached = _store_path(self.store_dir, role, self.model, sha) if self.store_dir else None
        if cached and cached.is_file():
            d = json.loads(cached.read_text())
            return _parse_json(d.get("text", ""))
        request = {"model": self.model, "max_tokens": self.max_output_tokens, "system": system,
                   "messages": [{"role": "user", "content": content}]}
        reply = self._transport_or_fail().create(request)
        # Only end_turn is a finished answer. max_tokens, refusal and model_context_window_exceeded all leave a
        # truncated or missing answer: treat every other stop as a hard stop for the document.
        if reply.stop_reason != "end_turn":
            raise ExtractionStop(f"{reply.stop_reason}: the model did not finish a usable answer")
        data = _parse_json(reply.text)                      # parse before caching: a bad reply is never stored
        if cached and self.record:
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_text(json.dumps({"stop_reason": reply.stop_reason, "text": reply.text, "usage": reply.usage},
                                         ensure_ascii=False, indent=2))
        return data

    # ---- prompt building -----------------------------------------------------

    def _content(self, document: Document, instruction: str) -> list[dict]:
        blocks: list[dict] = [{"type": "text", "text": instruction}]
        if document.text and document.text.strip():
            blocks.append({"type": "text", "text": "Document text:\n" + document.text[:self.max_input_chars]})
        sparse = not document.text or len(document.text.strip()) < 200 or bool(document.pages_without_text)
        if sparse and document.images:
            for media_type, data in document.images[:self.max_pages]:
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": media_type,
                                                           "data": base64.b64encode(data).decode("ascii")}})
        return blocks

    def classify(self, document: Document, kinds: list[Structure]) -> Routing:
        catalogue = [{"name": k.name, "description": (k.description or "")[:600]} for k in kinds]
        system = (
            "You route one supplier document to a known kind. You are given the kinds and the document. "
            "Answer with one JSON object and nothing else: {\"decision\": \"kind|none|ambiguous\", "
            "\"kind\": <name or null>, \"reason\": <short string>}. Use \"kind\" only when one kind clearly fits, "
            "\"ambiguous\" when two or more fit, \"none\" when nothing fits.")
        instruction = "Known kinds:\n" + json.dumps(catalogue, ensure_ascii=False) + f"\n\nFile path: {document.path}"
        data = self._answer("classify", document.sha256, system, self._content(document, instruction))
        decision = data.get("decision") if data.get("decision") in ("kind", "none", "ambiguous") else "none"
        kind = data.get("kind") if decision == "kind" else None
        return Routing(decision, kind, str(data.get("reason") or "")[:500], self.label)

    def extract(self, document: Document, structure: Structure, schema: dict, contract: dict) -> dict:
        vocabulary = [{"name": t.get("name"), "attributes": t.get("attributes")}
                      for t in (contract.get("fact_types") or [])]
        system = (
            "You read one supplier document into facts that follow a fixed contract. Answer with one JSON object "
            "and nothing else, with exactly these keys: doc_kind, doc_kind_confidence (high|medium|low), languages "
            "(array of ISO codes), doc_date (YYYY-MM-DD or null), facts (array), unreadable (array of strings), "
            "proposed_fact_types (array). Each fact is {fact_type, subject (object of entity ids, never a row or "
            "line number), attribute, value, unit, locator ({page, section}), evidence (the exact text you read "
            "the value from), confidence (high|medium|low)}. Copy every quantity verbatim as it appears in the "
            "document, keeping any digit grouping (for example \"10 000\"); do not round or reformat it. Use only "
            "fact types from the contract; for genuinely new information name the type x_<something> and list it in "
            "proposed_fact_types.")
        instruction = (f"Document kind: {structure.name}\nReading rules:\n{(structure.description or '')[:4000]}\n\n"
                       f"Contract fact types:\n{json.dumps(vocabulary, ensure_ascii=False)}")
        data = self._answer("extract", document.sha256, system, self._content(document, instruction))
        return _envelope(data, structure.name, _numeric_attrs(contract))

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
        if sample_rows:
            instruction += "\n\nSample rows:\n" + json.dumps(sample_rows[:30], ensure_ascii=False)
        data = self._answer("design", document.sha256, system, self._content(document, instruction))
        target = data.get("target")
        if target not in ("doc_kind", "schema_map"):
            target = "schema_map" if data.get("schema_map") else "doc_kind"
        return StructureProposal(target=target, name=str(data.get("name") or "").strip(),
                                 reason=str(data.get("reason") or "")[:500], description=data.get("description"),
                                 recognise=data.get("recognise"), schema_map=data.get("schema_map"))


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("the model did not return a JSON object")
        data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("the model returned JSON that is not an object")
    return data


def _coerce_number(value: str):
    compact = _WS.sub("", value.strip())
    if _INT.fullmatch(compact):
        return int(compact)
    if _GROUPED.fullmatch(compact):
        return int(compact.replace(".", "").replace(",", ""))
    if _DEC.fullmatch(compact):
        # A dot followed by exactly three digits ("10.000") is ambiguous between a decimal and a single
        # thousands group, so keep it as text rather than guess a wrong number.
        if len(compact.lstrip("+-").split(".")[1]) == 3:
            return None
        return float(compact)
    return None


def _numeric_attrs(contract: dict) -> dict:
    """Map each contract fact type to the set of its attributes declared `type: number`. Only those values are
    parsed into numbers; everything else (identifiers, dates, codes, phone numbers) is kept verbatim as text,
    so a value like "007" or a PO number is never retyped and stripped of its leading zero."""
    out: dict[str, set] = {}
    for t in contract.get("fact_types") or []:
        attrs = t.get("attributes") or {}
        out[t.get("name")] = {a for a, spec in attrs.items()
                              if isinstance(spec, dict) and spec.get("type") == "number"}
    return out


def _envelope(data: dict, default_kind: str, numeric_attrs: dict | None = None) -> dict:
    numeric_attrs = numeric_attrs or {}
    facts = []
    for f in data.get("facts") or []:
        if not isinstance(f, dict):
            continue
        f = dict(f)
        value = f.get("value")
        if isinstance(value, str) and f.get("attribute") in numeric_attrs.get(f.get("fact_type"), ()):
            number = _coerce_number(value)
            if number is not None:
                if f.get("evidence") is None:            # keep the verbatim text the number was read from
                    f["evidence"] = value
                f["value"] = number
        facts.append(f)
    env = {
        "source": {},
        "doc_kind": data.get("doc_kind") or default_kind,
        "doc_kind_confidence": data.get("doc_kind_confidence") or "medium",
        "languages": data.get("languages") if isinstance(data.get("languages"), list) else [],
        "spec_version": SPEC_VERSION,
        "extracted_by": "",
        "doc_date": data.get("doc_date"),
        "facts": facts,
        "unreadable": data.get("unreadable") if isinstance(data.get("unreadable"), list) else [],
        "proposed_fact_types": (data.get("proposed_fact_types")
                                if isinstance(data.get("proposed_fact_types"), list) else []),
    }
    return {k: env[k] for k in _ENVELOPE_KEYS}
