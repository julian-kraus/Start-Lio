"""Load and apply the JSON Schemas in plan/contract/: the interface between sourcing, agents and the app.

    from plan import contracts
    contracts.validate("recommendation", obj)     # raises contracts.ContractError
    contracts.validate_record(record)              # a Lookahead record, by its kind
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

CONTRACT_DIR = Path(__file__).resolve().parent / "contract"
EXAMPLES_DIR = CONTRACT_DIR / "examples"
RECORD_PREFIX = "record_"


class ContractError(ValueError):
    pass


@lru_cache(maxsize=1)
def _schemas() -> dict[str, dict]:
    return {p.name.removesuffix(".schema.json"): json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(CONTRACT_DIR.glob("*.schema.json"))}


@lru_cache(maxsize=1)
def _registry() -> Registry:
    return Registry().with_resources((s["$id"], Resource.from_contents(s)) for s in _schemas().values())


def names() -> list[str]:
    return list(_schemas())


def record_kinds() -> list[str]:
    return [n.removeprefix(RECORD_PREFIX) for n in _schemas() if n.startswith(RECORD_PREFIX)]


@lru_cache(maxsize=None)
def validator(name: str) -> Draft202012Validator:
    try:
        schema = _schemas()[name]
    except KeyError:
        raise ContractError(f"no contract named {name!r}") from None
    return Draft202012Validator(schema, registry=_registry(), format_checker=FormatChecker())


def errors(name: str, obj) -> list[str]:
    out = []
    for e in sorted(validator(name).iter_errors(obj), key=lambda e: list(e.absolute_path)):
        where = "/".join(str(p) for p in e.absolute_path) or "(root)"
        out.append(f"{where}: {e.message}")
    return out


def validate(name: str, obj) -> None:
    errs = errors(name, obj)
    if errs:
        raise ContractError(f"{name}: " + "; ".join(errs))


def validate_record(record: dict) -> None:
    kind = record.get("kind") if isinstance(record, dict) else None
    if kind not in record_kinds():
        raise ContractError(f"unknown record kind {kind!r}")
    validate(RECORD_PREFIX + kind, record)
