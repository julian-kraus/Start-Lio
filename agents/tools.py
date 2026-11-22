"""The tools an agent role may call, and the guard that checks a verdict against their results.

A `Tool` is a deterministic function with a JSON Schema the model is shown (Anthropic tool format). A
`ToolRegistry` holds the tools a role is given, hands the model their schemas, and dispatches a call by name.
Dispatch never raises into the agent: a bad call comes back as `{"error": ...}` so the model can recover.

Two sources of tools:
- `sourcing/`: deterministic pure functions (coverage, timing, reliability, hedge, approvals). They return the
  numbers a decision rests on, so they are marked `grounds_numbers=True` and the guard checks the verdict against
  them. `sourcing/` is still being built, so a role registers the functions it needs with `registry.add(...)`.
- `mail.tools.MailTools`: read and draft tools for supplier email. They carry no numbers the guard grounds, and
  the send path is never exposed to a model (approving and sending are human-only, see AGENTS.md).

The guard (`GroundingGuard`) enforces the probe finding: a model that is free to compute numbers itself skips the
tool and answers with wrong or zero numbers, so every number a verdict reports must equal a value a grounding
tool returned. `check` lists the fields that do not; the loop in `agents.llm` uses that to reject and retry, then
to fall back to the tool result.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Callable

# Mail tools whose effect is to send or approve are never given to a model: a human approves and sends, by name.
MAIL_HUMAN_ONLY = frozenset({"send_approved"})


def _jsonable(obj: Any) -> Any:
    return json.loads(json.dumps(obj, default=str, ensure_ascii=False))


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    fn: Callable[..., Any]
    grounds_numbers: bool = False     # True when the result carries numbers a verdict must be checked against

    def schema(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None):
        self._tools: dict[str, Tool] = {}
        for t in tools or []:
            self.register(t)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} already registered")
        self._tools[tool.name] = tool

    def add(self, name: str, description: str, input_schema: dict, fn: Callable[..., Any],
            grounds_numbers: bool = False) -> Tool:
        tool = Tool(name, description, input_schema, fn, grounds_numbers)
        self.register(tool)
        return tool

    def names(self) -> list[str]:
        return list(self._tools)

    def grounding_names(self) -> set[str]:
        return {n for n, t in self._tools.items() if t.grounds_numbers}

    def schemas(self) -> list[dict]:
        return [t.schema() for t in self._tools.values()]

    def call(self, name: str, args: dict) -> dict:
        """Run one tool. Any failure (unknown tool, bad arguments, the tool raising) comes back as an error dict
        the model can read, never as an exception into the loop."""
        tool = self._tools.get(name)
        if tool is None:
            return {"error": f"no tool named {name!r}"}
        if not isinstance(args, dict):
            return {"error": f"tool {name!r} needs a JSON object of arguments"}
        try:
            result = tool.fn(**args)
        except TypeError as exc:
            return {"error": f"bad arguments for {name!r}: {exc}"}
        except Exception as exc:                       # a tool must not take the whole run down
            return {"error": f"{name!r} failed: {type(exc).__name__}: {exc}"}
        out = _jsonable(result)
        return out if isinstance(out, dict) else {"result": out}


# ---- exposing MailTools ------------------------------------------------------

def mail_tools(mail, *, exclude: frozenset[str] = MAIL_HUMAN_ONLY) -> list[Tool]:
    """Wrap `mail.tools.MailTools` methods as tools from the shipped Anthropic schemas in `mail.tool_schemas`.
    The send path is excluded by default. `mail` is a MailTools instance; each tool dispatches to the method of
    the same name."""
    from mail import tool_schemas
    out = []
    for spec in tool_schemas.TOOLS:
        name = spec["name"]
        if name in exclude:
            continue
        method = getattr(mail, name, None)
        if method is None:
            continue
        out.append(Tool(name, spec["description"], spec["input_schema"], _bind(method), grounds_numbers=False))
    return out


def _bind(method: Callable[..., Any]) -> Callable[..., Any]:
    def call(**kwargs):
        return method(**kwargs)
    return call


# ---- the grounding guard -----------------------------------------------------

@dataclass
class GroundingGuard:
    """Check a verdict's numbers against the grounding tool results.

    `fields` maps a verdict key to the key it must equal in a grounding tool's output (e.g. the verdict's
    `expected_loss_eur` must equal the hedge tool's `expected_loss_eur`). Grounding outputs are merged in call
    order, last write wins, so the latest value of a repeated key is the one checked. A verdict value within
    `tol` (relative for large numbers) of the tool value passes; anything else is a mismatch.
    """
    fields: dict[str, str]
    tol: float = 0.01
    rel_tol: float = 1e-6

    def sources(self, tool_outputs: list[dict]) -> dict:
        merged: dict = {}
        for out in tool_outputs:
            if isinstance(out, dict) and "error" not in out:
                merged.update(out)
        return merged

    def _close(self, a: Any, b: Any) -> bool:
        if isinstance(a, bool) or isinstance(b, bool):
            return bool(a) == bool(b)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            if not (math.isfinite(a) and math.isfinite(b)):
                return False
            return abs(a - b) <= max(self.tol, self.rel_tol * max(abs(a), abs(b)))
        return a == b

    def check(self, verdict: dict, tool_outputs: list[dict]) -> list[str]:
        """Return a list of human-readable mismatches; empty means the verdict is grounded."""
        if not isinstance(verdict, dict):
            return ["the verdict is not a JSON object"]
        src = self.sources(tool_outputs)
        problems = []
        for vkey, tkey in self.fields.items():
            if vkey not in verdict:
                continue                        # a field the verdict did not report is not this guard's concern
            if tkey not in src:
                problems.append(f"{vkey}: no grounding tool returned {tkey!r}; call the tool before answering")
                continue
            if not self._close(verdict.get(vkey), src[tkey]):
                problems.append(f"{vkey}={verdict.get(vkey)!r} does not match the tool result {tkey}={src[tkey]!r}")
        return problems

    def apply(self, verdict: dict, tool_outputs: list[dict]) -> dict:
        """The deterministic result wins: overwrite each mapped verdict field with the tool value when it exists.
        Used once the guard's retries are spent, so the answer that is stored carries the tool's numbers."""
        src = self.sources(tool_outputs)
        fixed = dict(verdict) if isinstance(verdict, dict) else {}
        for vkey, tkey in self.fields.items():
            if tkey in src:
                fixed[vkey] = src[tkey]
        return fixed
