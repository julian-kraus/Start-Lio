"""Settings, read from the environment (and an optional .env file)."""

from __future__ import annotations

import os
import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$")
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def load_env(path: Path) -> None:
    """Load KEY=VALUE lines into os.environ without overriding variables that are already set."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def parse_duration(text: str) -> float:
    match = _DURATION.match(text)
    if not match:
        raise ValueError(f"invalid duration: {text!r} (use e.g. 90s, 30m, 6h, 2d)")
    return float(match.group(1)) * _UNITS[match.group(2)]


@dataclass(frozen=True)
class StaleRule:
    pattern: str
    seconds: float


def parse_stale_rules(text: str) -> tuple[StaleRule, ...]:
    """Format: 'glob=duration,glob=duration', e.g. 'soony/erp/open_shipments.*=6h'."""
    rules = []
    for chunk in filter(None, (c.strip() for c in text.split(","))):
        pattern, _, duration = chunk.rpartition("=")
        if not pattern:
            raise ValueError(f"invalid stale rule: {chunk!r} (use glob=duration)")
        rules.append(StaleRule(pattern.strip(), parse_duration(duration)))
    return tuple(rules)


@dataclass(frozen=True)
class Settings:
    base_url: str
    token: str
    database_url: str
    data_dir: Path
    poll_interval: float
    stale_rules: tuple[StaleRule, ...]
    webhook_urls: tuple[str, ...]
    download_workers: int
    diff_max_items: int


def database_url() -> str | None:
    """DATABASE_URL, or one built from the POSTGRES_* variables (secrets/db.env)."""
    env = os.environ
    if env.get("DATABASE_URL"):
        return env["DATABASE_URL"]
    if not env.get("POSTGRES_PASSWORD"):
        return None
    user = urllib.parse.quote(env.get("POSTGRES_USER", "tracker"))
    password = urllib.parse.quote(env["POSTGRES_PASSWORD"], safe="")
    host = env.get("POSTGRES_HOST", "localhost")
    port = env.get("POSTGRES_PORT", "5433")
    return f"postgresql://{user}:{password}@{host}:{port}/{env.get('POSTGRES_DB', 'tracker')}"


def load_settings() -> Settings:
    load_env(PROJECT_ROOT / ".env")
    load_env(PROJECT_ROOT / "secrets" / "db.env")
    env = os.environ
    db_url = database_url()
    missing = [k for k in ("SOONY_API_BASE_URL", "SOONY_API_TOKEN") if not env.get(k)]
    if not db_url:
        missing.append("DATABASE_URL or POSTGRES_PASSWORD")
    if missing:
        raise SystemExit(f"missing required settings: {', '.join(missing)}")
    return Settings(
        base_url=env["SOONY_API_BASE_URL"].rstrip("/"),
        token=env["SOONY_API_TOKEN"],
        database_url=db_url,
        data_dir=Path(env.get("TRACKER_DATA_DIR", PROJECT_ROOT / "data")),
        poll_interval=parse_duration(env.get("POLL_INTERVAL", "60s")),
        stale_rules=parse_stale_rules(env.get("STALE_RULES", "")),
        webhook_urls=tuple(u.strip() for u in env.get("WEBHOOK_URLS", "").split(",") if u.strip()),
        download_workers=int(env.get("DOWNLOAD_WORKERS", "8")),
        diff_max_items=int(env.get("DIFF_MAX_ITEMS", "500")),
    )
