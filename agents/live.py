"""Live probes for the production agent loop.

The probe is kept beside the production adapter so the shared live-run harness only
loads inputs, records usage, and serializes the result.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def run_agent_tools_probe(*, key: str, model: str, n: int, tool_specs: list[dict],
                          tool_values: dict[str, Any], hedge_math: Callable[..., dict],
                          verdict_schema: dict, system: str, prompt: str,
                          max_output_tokens: int = 8000) -> tuple[dict, dict]:
    """Run the production tool-use loop and return its report and token usage."""
    from .llm import AnthropicClient, run as run_agent_role
    from .tools import GroundingGuard, ToolRegistry

    specs = {tool["name"]: tool for tool in tool_specs}
    registry = ToolRegistry()
    registry.add("hedge_math", specs["hedge_math"]["description"],
                  specs["hedge_math"]["input_schema"], lambda **kw: hedge_math(**kw),
                  grounds_numbers=True)
    for name in ("get_shortfall", "get_reliability", "get_offer", "get_goal"):
        registry.add(name, specs[name]["description"], specs[name]["input_schema"],
                     (lambda selected: (lambda **_kw: tool_values[selected]))(name))

    guard = GroundingGuard({"hedge": "hedge", "expected_loss_eur": "expected_loss_eur",
                            "break_even_p_fail": "break_even_p_fail"})
    client = AnthropicClient(api_key=key, model=model, max_output_tokens=max_output_tokens)
    expected = hedge_math(0.35, 3100, 90, 0.25 * 4000 * 3.5)
    usage = {"input": 0, "output": 0, "calls": 0}
    runs = []
    for _ in range(n):
        result = run_agent_role(client, registry, role="hedger", system=system, prompt=prompt,
                                schema=verdict_schema, guard=guard)
        verdict = result.verdict or {}
        checks = {
            "answered": result.ok,
            "used_hedge_math": "hedge_math" in result.calls,
            "guard_held_without_fallback": result.ok and not result.guard_failed,
            "hedge_matches_tool": verdict.get("hedge") == expected["hedge"],
            "expected_loss_matches_tool": abs(verdict.get("expected_loss_eur", -1)
                                               - expected["expected_loss_eur"]) < 0.01,
        }
        usage["input"] += result.usage.get("input_tokens", 0)
        usage["output"] += result.usage.get("output_tokens", 0)
        usage["calls"] += 1
        runs.append({"calls": result.calls, "guard_retries": result.guard_retries,
                     "guard_failed": result.guard_failed, "verdict": verdict, "checks": checks})
    report = {"expected": expected, "runs": runs,
              "all_checks_pass": sum(all(run["checks"].values()) for run in runs), "of": n}
    return report, usage
