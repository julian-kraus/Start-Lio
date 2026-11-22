"""Discover additive Lookahead API modules.

Each ``tracker.lookahead_api_*.py`` module exports ``ROUTES``. A route is a
``(method, pattern, handler)`` tuple where ``pattern`` is a regular expression
and ``handler(routes, lookahead, match, params, body)`` returns the response
payload or raises ``LookaheadError``. The registry is deliberately discovered
at runtime so a view can add an endpoint without editing the host handler.
"""

from __future__ import annotations

import importlib
import pkgutil
import re
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Callable, Iterable


Handler = Callable[..., Any]


@dataclass(frozen=True)
class Route:
    method: str
    pattern: re.Pattern[str]
    handler: Handler


def _items(raw: Any) -> Iterable[tuple[str, Any, Handler]]:
    if isinstance(raw, dict):
        for method, routes in raw.items():
            if isinstance(routes, dict):
                for pattern, handler in routes.items():
                    yield str(method), pattern, handler
            else:
                yield from ((str(method), pattern, handler) for pattern, handler in routes)
        return
    for item in raw or ():
        if isinstance(item, dict):
            yield str(item["method"]), item["pattern"], item["handler"]
        else:
            method, pattern, handler = item
            yield str(method), pattern, handler


def _module_routes(module: ModuleType) -> list[Route]:
    return [Route(method.upper(), re.compile(pattern) if isinstance(pattern, str) else pattern, handler)
            for method, pattern, handler in _items(getattr(module, "ROUTES", ()))]


def load_routes(package: str = "tracker") -> tuple[Route, ...]:
    """Import every sibling ``lookahead_api_*.py`` and return its routes."""
    root = importlib.import_module(package)
    prefix = package + "."
    found: list[Route] = []
    for info in sorted(pkgutil.iter_modules(root.__path__, prefix), key=lambda item: item.name):
        if info.name.startswith(prefix + "lookahead_api_"):
            found.extend(_module_routes(importlib.import_module(info.name)))
    return tuple(found)


def match_route(method: str, path: str, package: str = "tracker") -> tuple[Route, re.Match[str]] | None:
    """Return the first matching registered route, if any."""
    for route in load_routes(package):
        if route.method == method.upper():
            match = route.pattern.fullmatch(path)
            if match:
                return route, match
    return None
