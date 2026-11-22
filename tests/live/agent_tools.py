"""Live probe adapter for the shipped agent tool loop."""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Any, Callable


def run_agent_tools(
    *,
    root: Path,
    key: str,
    model: str,
    n: int,
    tool_specs: list[dict[str, Any]],
    tool_values: dict[str, Any],
    hedge_math: Callable[..., dict],
    verdict_schema: dict[str, Any],
    system: str,
    prompt: str,
) -> tuple[dict[str, Any], dict[str, int]]:
    """Run the production probe with the harness-owned synthetic inputs."""
    sys.path.insert(0, str(root))
    from agents.live import run_agent_tools_probe

    return run_agent_tools_probe(
        key=key,
        model=model,
        n=n,
        tool_specs=tool_specs,
        tool_values=tool_values,
        hedge_math=hedge_math,
        verdict_schema=verdict_schema,
        system=system,
        prompt=prompt,
    )
