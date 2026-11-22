"""The LLM adapter for agent roles and the tool-use loop that drives one role to a grounded verdict.

Adapters (all `LLMClient`):
- `AnthropicClient`: the live path. Key from LOOKAHEAD_ANTHROPIC_API_KEY, model from LOOKAHEAD_LLM_MODEL. The
  official `anthropic` SDK is imported only when a call is actually made. It refuses to start without a key
  instead of falling back silently to some empty adapter (a replay adapter is a separate, explicit choice).
- `ReplayClient`: the test path. It plays a recorded script of replies (tool calls, then a final JSON answer) so
  the whole loop, including the guard and its retries, runs without a key or the network.

The loop (`run`) follows the probe finding built into tests/live/llm_eval.py: a role must take every number it
reports from a tool. It calls the tools the model asks for, then checks the final verdict with the `GroundingGuard`.
A verdict whose numbers do not match the tool results is rejected with the reason and retried, at most twice; after
that the deterministic tool result wins and the trace records that the guard failed. Every model turn and tool call
is written to `plan.agent_traces` through a `Tracer`.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tracker.config import PROJECT_ROOT, load_env

from .tools import GroundingGuard, ToolRegistry
from .traces import Tracer

DEFAULT_MODEL = "claude-haiku-5-5"


class AgentStop(RuntimeError):
    """A hard stop from the provider (timeout, connection): the run cannot finish this time."""


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class AgentReply:
    stop_reason: str
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    raw_content: Any = None          # the assistant content to append back to the conversation verbatim

    def assistant_content(self) -> Any:
        if self.raw_content is not None:
            return self.raw_content
        blocks: list[dict] = []
        if self.text:
            blocks.append({"type": "text", "text": self.text})
        for c in self.tool_calls:
            blocks.append({"type": "tool_use", "id": c.id, "name": c.name, "input": c.input})
        return blocks


class LLMClient:
    name: str = "llm"
    model: str | None = None

    def message(self, *, system: str, messages: list[dict], tools: list[dict] | None = None,
                schema: dict | None = None) -> AgentReply:
        raise NotImplementedError

    @property
    def label(self) -> str:
        return f"llm:{self.name}" + (f"/{self.model}" if self.model else "")


# ---- live adapter ------------------------------------------------------------

class ApiTransport:
    """The live path: the official `anthropic` SDK, imported only when a call is actually made."""

    def __init__(self, api_key: str, timeout: float, max_retries: int = 2):
        import anthropic

        self._errors = anthropic
        self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=max_retries)

    def create(self, request: dict) -> AgentReply:
        try:
            msg = self._client.messages.create(**request)
        except self._errors.APITimeoutError as exc:
            raise AgentStop(f"timeout: {exc}") from exc
        except self._errors.APIConnectionError as exc:
            raise AgentStop(f"connection: {exc}") from exc
        text = "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", None) == "text")
        calls = [ToolCall(b.id, b.name, dict(b.input or {}))
                 for b in msg.content if getattr(b, "type", None) == "tool_use"]
        usage = {"input_tokens": getattr(msg.usage, "input_tokens", None),
                 "output_tokens": getattr(msg.usage, "output_tokens", None)} if msg.usage else {}
        return AgentReply(msg.stop_reason or "end_turn", text, calls, usage, raw_content=msg.content)


class AnthropicClient(LLMClient):
    name = "anthropic"

    def __init__(self, *, api_key: str | None = None, model: str | None = None, transport: ApiTransport | None = None,
                 timeout: float | None = None, max_output_tokens: int | None = None, effort: str | None = None,
                 max_retries: int = 2):
        load_env(PROJECT_ROOT / "secrets" / "anthropic.env")
        env = os.environ
        self.model = model or env.get("LOOKAHEAD_LLM_MODEL") or DEFAULT_MODEL
        self._api_key = api_key or env.get("LOOKAHEAD_ANTHROPIC_API_KEY")
        self._timeout = timeout if timeout is not None else float(env.get("LOOKAHEAD_LLM_TIMEOUT", "120"))
        self.max_output_tokens = (max_output_tokens if max_output_tokens is not None
                                  else int(env.get("LOOKAHEAD_AGENT_MAX_TOKENS", "4096")))
        self.effort = effort or env.get("LOOKAHEAD_AGENT_EFFORT", "medium")
        self._max_retries = max_retries
        self._transport = transport
        # Refuse to start without a key rather than fall back silently: a key-less run would quietly produce
        # nothing or, worse, numbers no tool grounded. Use ReplayClient for the key-less test path on purpose.
        if self._transport is None and not self._api_key:
            raise RuntimeError("no LOOKAHEAD_ANTHROPIC_API_KEY set; refusing to start the Anthropic agent adapter "
                               "(use agents.llm.ReplayClient for tests)")

    def _transport_or_fail(self) -> ApiTransport:
        if self._transport is None:
            self._transport = ApiTransport(self._api_key, self._timeout, self._max_retries)
        return self._transport

    def message(self, *, system: str, messages: list[dict], tools: list[dict] | None = None,
                schema: dict | None = None) -> AgentReply:
        output_config: dict = {"effort": self.effort}
        if schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        request: dict = {"model": self.model, "max_tokens": self.max_output_tokens, "system": system,
                         "messages": messages, "output_config": output_config}
        if tools:
            request["tools"] = tools
        return self._transport_or_fail().create(request)


# ---- replay adapter ----------------------------------------------------------

class ReplayClient(LLMClient):
    """Play a recorded script of replies. Each entry is a dict:
        {"stop_reason": "tool_use"|"end_turn"|..., "text": "...",
         "tool_calls": [{"name": ..., "input": {...}}], "usage": {"input_tokens": .., "output_tokens": ..}}
    Tool-use ids are synthesized. Raises when the script is exhausted so a test cannot pass by accident.
    """
    name = "replay"

    def __init__(self, script: list[dict] | str | os.PathLike, model: str = "replay"):
        if isinstance(script, (str, os.PathLike)):
            script = json.loads(Path(script).read_text())
        self._script = list(script)
        self._i = 0
        self.model = model

    def message(self, *, system: str, messages: list[dict], tools: list[dict] | None = None,
                schema: dict | None = None) -> AgentReply:
        if self._i >= len(self._script):
            raise AgentStop(f"replay script exhausted after {self._i} replies")
        entry = self._script[self._i]
        self._i += 1
        calls = [ToolCall(f"replay-{self._i}-{j}", c["name"], dict(c.get("input") or {}))
                 for j, c in enumerate(entry.get("tool_calls") or [])]
        stop = entry.get("stop_reason") or ("tool_use" if calls else "end_turn")
        return AgentReply(stop, entry.get("text", ""), calls, entry.get("usage") or {})


# ---- the agent loop ----------------------------------------------------------

@dataclass
class AgentResult:
    verdict: Any = None
    text: str = ""
    calls: list[str] = field(default_factory=list)
    tool_outputs: list[dict] = field(default_factory=list)
    guard_retries: int = 0
    guard_failed: bool = False       # True when the guard's retries were spent and the tool result was forced in
    stop_reason: str | None = None
    error: str | None = None
    run_id: Any = None
    usage: dict = field(default_factory=dict)    # summed input/output tokens across the run's model turns

    @property
    def ok(self) -> bool:
        return self.error is None


def _rejection(problems: list[str]) -> str:
    return ("Rejected by the validator: every number in your answer must come from a tool result, not computed by "
            "you. " + " ".join(problems) + " Call the tool(s) with the right inputs, then answer again.")


def _parse_json(text: str) -> dict:
    text = (text or "").strip()
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


def run(client: LLMClient, registry: ToolRegistry, *, role: str, system: str, prompt: str,
        schema: dict | None = None, guard: GroundingGuard | None = None, tracer: Tracer | None = None,
        max_turns: int = 12, max_guard_retries: int = 2) -> AgentResult:
    """Drive one role: call the tools it asks for, then check its verdict with the guard. On a mismatch, reject
    and retry up to `max_guard_retries`; after that the deterministic tool result wins and the trace is marked."""
    messages: list[dict] = [{"role": "user", "content": prompt}]
    tool_outputs: list[dict] = []
    grounded: list[dict] = []
    calls: list[str] = []
    guard_retries = 0
    run_id = tracer.run_id if tracer else None
    tools = registry.schemas() or None
    usage = {"input_tokens": 0, "output_tokens": 0}

    for _ in range(max_turns):
        step = tracer.next_step() if tracer else 0
        try:
            reply = client.message(system=system, messages=messages, tools=tools, schema=schema)
        except AgentStop as exc:
            if tracer:
                tracer.trace(step=step, error=str(exc))
                tracer.finish("failed", error=str(exc))
            return AgentResult(calls=calls, tool_outputs=tool_outputs, error=str(exc), run_id=run_id, usage=usage)
        ti, to = reply.usage.get("input_tokens"), reply.usage.get("output_tokens")
        usage["input_tokens"] += ti or 0
        usage["output_tokens"] += to or 0

        if reply.stop_reason == "tool_use" and reply.tool_calls:
            messages.append({"role": "assistant", "content": reply.assistant_content()})
            if tracer:
                tracer.trace(step=step, output={"requested": [c.name for c in reply.tool_calls]},
                             model=client.model, tokens_in=ti, tokens_out=to)
            results = []
            for c in reply.tool_calls:
                calls.append(c.name)
                out = registry.call(c.name, c.input)
                tool_outputs.append(out)
                if c.name in registry.grounding_names():
                    grounded.append(out)
                results.append({"type": "tool_result", "tool_use_id": c.id,
                                "content": json.dumps(out, ensure_ascii=False, default=str)})
                if tracer:
                    tracer.trace(step=step, tool=c.name, input=c.input, output=out, model=client.model)
            messages.append({"role": "user", "content": results})
            continue

        if reply.stop_reason != "end_turn":
            err = f"the model stopped with {reply.stop_reason}, no usable answer"
            if tracer:
                tracer.trace(step=step, output={"stop_reason": reply.stop_reason}, model=client.model,
                             tokens_in=ti, tokens_out=to, error=err)
                tracer.finish("failed", error=err)
            return AgentResult(calls=calls, tool_outputs=tool_outputs, stop_reason=reply.stop_reason,
                               error=err, run_id=run_id, usage=usage)

        try:
            verdict: Any = _parse_json(reply.text) if (schema or (reply.text or "").strip().startswith("{")) \
                else {"text": reply.text}
        except ValueError as exc:
            err = f"could not read the answer as JSON: {exc}"
            if tracer:
                tracer.trace(step=step, output={"text": reply.text[:500]}, model=client.model,
                             tokens_in=ti, tokens_out=to, error=err)
                tracer.finish("failed", error=err)
            return AgentResult(text=reply.text, calls=calls, tool_outputs=tool_outputs, error=err,
                               run_id=run_id, usage=usage)

        problems = guard.check(verdict, grounded) if guard else []
        if problems and guard_retries < max_guard_retries:
            guard_retries += 1
            if tracer:
                tracer.trace(step=step, output=verdict, model=client.model, tokens_in=ti, tokens_out=to,
                             verdict_ok=False, error="; ".join(problems))
            messages.append({"role": "assistant", "content": reply.assistant_content()})
            messages.append({"role": "user", "content": _rejection(problems)})
            continue
        if problems:
            missing = guard.ungrounded(verdict, grounded)
            fixed = guard.apply(verdict, grounded)
            if missing:                     # no tool result exists for these: refuse, never pass the model's number
                err = (f"guard failed after {guard_retries} retries; no grounding tool result for "
                       f"{', '.join(missing)}, verdict refused")
                if tracer:
                    tracer.trace(step=step, output=fixed, model=client.model, tokens_in=ti, tokens_out=to,
                                 verdict_ok=False, error=err)
                    tracer.finish("failed", error=err)
                return AgentResult(verdict=fixed, text=reply.text, calls=calls, tool_outputs=tool_outputs,
                                   guard_retries=guard_retries, guard_failed=True, stop_reason="end_turn",
                                   error=err, run_id=run_id, usage=usage)
            err = f"guard failed after {guard_retries} retries; tool result used: " + "; ".join(problems)
            if tracer:
                tracer.trace(step=step, output=fixed, model=client.model, tokens_in=ti, tokens_out=to,
                             verdict_ok=False, error=err)
                tracer.finish("done", output=fixed)
            return AgentResult(verdict=fixed, text=reply.text, calls=calls, tool_outputs=tool_outputs,
                               guard_retries=guard_retries, guard_failed=True, stop_reason="end_turn",
                               run_id=run_id, usage=usage)

        if tracer:
            tracer.trace(step=step, output=verdict, model=client.model, tokens_in=ti, tokens_out=to, verdict_ok=True)
            tracer.finish("done", output=verdict)
        return AgentResult(verdict=verdict, text=reply.text, calls=calls, tool_outputs=tool_outputs,
                           guard_retries=guard_retries, stop_reason="end_turn", run_id=run_id, usage=usage)

    err = f"no final answer after {max_turns} turns"
    if tracer:
        tracer.finish("failed", error=err)
    return AgentResult(calls=calls, tool_outputs=tool_outputs, error=err, run_id=run_id, usage=usage)
