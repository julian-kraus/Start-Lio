"""Webhook delivery for consumers outside the compose network. Batched per poll, cursor-based retries."""

from __future__ import annotations

import hashlib
import json
import logging
import urllib.parse
import urllib.request

from . import db

log = logging.getLogger(__name__)
BATCH = 200


def _serialize(row: dict) -> dict:
    return {**row, "at": row["at"].isoformat()}


def redact(url: str) -> str:
    """scheme://host/path only: query strings and credentials often carry tokens."""
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.hostname or ''}{f':{parts.port}' if parts.port else ''}{parts.path}"


def cursor_name(url: str) -> str:
    return f"webhook:{redact(url)}#{hashlib.sha256(url.encode()).hexdigest()[:12]}"


def dispatch_webhooks(conn, urls: tuple[str, ...], timeout: float = 10) -> None:
    """POST {"events": [...]} to each URL. On failure the cursor stays put and the batch is retried next poll."""
    for url in urls:
        name = cursor_name(url)
        try:
            cursor = db.get_cursor(conn, name, start="latest")
            conn.commit()
            while rows := db.fetch_events(conn, cursor, BATCH):
                body = json.dumps({"events": [_serialize(r) for r in rows]}, ensure_ascii=False).encode()
                req = urllib.request.Request(url, data=body, method="POST",
                                             headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    resp.read()
                cursor = rows[-1]["id"]
                db.set_cursor(conn, name, cursor)
                conn.commit()
            conn.commit()
        except Exception as exc:
            conn.rollback()
            log.warning("webhook %s failed, will retry next poll: %s", redact(url), type(exc).__name__)
