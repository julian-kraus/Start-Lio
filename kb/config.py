"""Settings for the kb layer. Database credentials come from the same environment as the tracker."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from tracker.config import PROJECT_ROOT, database_url, load_env

SCHEMA_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


@dataclass(frozen=True)
class KbSettings:
    database_url: str
    schema: str
    data_dir: Path
    structured_dir: Path
    seed_path: Path
    llm_adapter: str | None


def load_settings() -> KbSettings:
    load_env(PROJECT_ROOT / ".env")
    load_env(PROJECT_ROOT / "secrets" / "db.env")
    env = os.environ
    url = database_url()
    if not url:
        raise SystemExit("missing DATABASE_URL or POSTGRES_PASSWORD (see secrets/db.env)")
    schema = env.get("KB_SCHEMA", "kb")
    if not SCHEMA_NAME.match(schema):
        raise SystemExit(f"invalid KB_SCHEMA {schema!r}")
    return KbSettings(
        database_url=url,
        schema=schema,
        data_dir=Path(env.get("TRACKER_DATA_DIR", PROJECT_ROOT / "data")),
        structured_dir=Path(env.get("KB_STRUCTURED_DIR", PROJECT_ROOT / "structured")),
        seed_path=Path(env.get("KB_RULES_SEED", Path(__file__).resolve().parent / "rules.seed.yaml")),
        llm_adapter=env.get("KB_LLM_ADAPTER") or None,
    )
