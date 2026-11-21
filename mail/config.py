"""Settings for the mail layer, read from the environment, .env, secrets/db.env and secrets/mail.env."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from tracker.config import PROJECT_ROOT, database_url, load_env, parse_duration

SCHEMA_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
SEND_MODES = ("dry_run", "test_only", "live")


@dataclass(frozen=True)
class MailSettings:
    database_url: str | None
    schema: str
    address: str
    password: str = field(repr=False)
    display_name: str
    send_mode: str
    sender_registered: bool
    test_addresses: tuple[str, ...]
    imap_host: str
    imap_port: int
    smtp_host: str
    smtp_port: int
    poll_interval: float
    backfill_days: int
    data_dir: Path
    tracker_data_dir: Path
    approval_ttl: float
    rate_per_supplier_hour: int
    rate_global_hour: int
    min_gap_per_recipient: float
    tag_prefix: str
    kb_schema: str
    kb_email_attr: str
    kb_language_attr: str
    kb_name_attr: str

    @property
    def domain(self) -> str:
        return self.address.rsplit("@", 1)[-1] if "@" in self.address else "localhost"

    def require_db(self) -> str:
        if not self.database_url:
            raise SystemExit("missing DATABASE_URL or POSTGRES_PASSWORD (see secrets/db.env)")
        return self.database_url

    def require_credentials(self) -> None:
        missing = [k for k, v in (("MAIL_ADDRESS", self.address), ("MAIL_APP_PASSWORD", self.password)) if not v]
        if missing:
            raise SystemExit(f"missing in secrets/mail.env: {', '.join(missing)}")


def _bool(text: str | None) -> bool:
    return (text or "").strip().lower() in ("1", "yes", "true", "on")


def load_settings(load_files: bool = True) -> MailSettings:
    if load_files:
        load_env(PROJECT_ROOT / ".env")
        load_env(PROJECT_ROOT / "secrets" / "db.env")
        load_env(PROJECT_ROOT / "secrets" / "mail.env")
    env = os.environ
    schema = env.get("MAIL_SCHEMA", "mail")
    kb_schema = env.get("KB_SCHEMA", "kb")
    for name in (schema, kb_schema):
        if not SCHEMA_NAME.match(name):
            raise SystemExit(f"invalid schema name {name!r}")
    mode = env.get("MAIL_SEND_MODE", "dry_run").strip()
    if mode not in SEND_MODES:
        raise SystemExit(f"MAIL_SEND_MODE must be one of {SEND_MODES}, got {mode!r}")
    smtp_port = int(env.get("MAIL_SMTP_PORT", "465"))
    return MailSettings(
        database_url=database_url(),
        schema=schema,
        address=env.get("MAIL_ADDRESS", "").strip().lower(),
        # Google shows app passwords in groups of four; the spaces are not part of it.
        password=re.sub(r"\s+", "", env.get("MAIL_APP_PASSWORD", "")),
        display_name=env.get("MAIL_DISPLAY_NAME", "").strip(),
        send_mode=mode,
        sender_registered=_bool(env.get("MAIL_SENDER_REGISTERED")),
        test_addresses=tuple(a.strip().lower() for a in env.get("MAIL_TEST_ADDRESSES", "").split(",") if a.strip()),
        imap_host=env.get("MAIL_IMAP_HOST", "imap.gmail.com"),
        imap_port=int(env.get("MAIL_IMAP_PORT", "993")),
        smtp_host=env.get("MAIL_SMTP_HOST", "smtp.gmail.com"),
        smtp_port=smtp_port,
        poll_interval=parse_duration(env.get("MAIL_POLL_INTERVAL", "30s")),
        backfill_days=int(env.get("MAIL_BACKFILL_DAYS", "3")),
        data_dir=Path(env.get("MAIL_DATA_DIR", PROJECT_ROOT / "mail_data")),
        tracker_data_dir=Path(env.get("TRACKER_DATA_DIR", PROJECT_ROOT / "data")),
        approval_ttl=parse_duration(env.get("MAIL_APPROVAL_TTL", "12h")),
        rate_per_supplier_hour=int(env.get("MAIL_RATE_PER_SUPPLIER_HOUR", "3")),
        rate_global_hour=int(env.get("MAIL_RATE_GLOBAL_HOUR", "15")),
        min_gap_per_recipient=parse_duration(env.get("MAIL_MIN_GAP_PER_RECIPIENT", "60s")),
        tag_prefix=env.get("MAIL_TAG_PREFIX", "GH6").strip().upper(),
        kb_schema=kb_schema,
        kb_email_attr=env.get("MAIL_KB_EMAIL_ATTR", "contact_email"),
        kb_language_attr=env.get("MAIL_KB_LANGUAGE_ATTR", "preferred_language"),
        kb_name_attr=env.get("MAIL_KB_NAME_ATTR", "contact_name"),
    )
