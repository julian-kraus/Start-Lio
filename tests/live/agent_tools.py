"""Live probe adapter for the shipped agent tool loop."""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Any, Callable

# Synthetic probe wording (no data values): instructs the model to ground every number in a tool result.
_SYSTEM = ("You decide whether to place a backup order. Get every input from the tools. Every number "
           "in your answer must come from a tool result; never compute numbers yourself. Cite the goal line.")
_PROMPT = ("Part P-ALPHA is short for the premium edition. Should we hedge with the backup offer? The "
           "commitment now is the deposit share of the backup order value.")


def test_agent_tools(
    *,
    root: Path,
    api_key: str,
    model: str,
    n: int,
    result_key: str,
    tool_specs: list[dict[str, Any]],
    tool_values: dict[str, Any],
    hedge_math: Callable[..., dict],
    verdict_schema: dict[str, Any],
    results: dict[str, Any],
    usage_total: dict[str, int],
    system: str = _SYSTEM,
    prompt: str = _PROMPT,
) -> None:
    """Run the production probe with the harness-owned synthetic inputs."""
    sys.path.insert(0, str(root))
    from agents.live import run_agent_tools_probe

    report, usage = run_agent_tools_probe(
        key=api_key,
        model=model,
        n=n,
        tool_specs=tool_specs,
        tool_values=tool_values,
        hedge_math=hedge_math,
        verdict_schema=verdict_schema,
        system=system,
        prompt=prompt,
    )
    for name, value in usage.items():
        usage_total[name] += value
    results["tests"][result_key] = report
