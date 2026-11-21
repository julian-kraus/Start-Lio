"""HTTP client for the portal API with keep-alive connections and retries."""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import os
import random
import tempfile
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

CHUNK = 1 << 16
RETRY_STATUS = {429, 500, 502, 503, 504}


class PortalError(RuntimeError):
    pass


class AuthError(PortalError):
    pass


class IntegrityError(PortalError):
    """The downloaded content does not match the size or checksum the listing announced."""


@dataclass(frozen=True)
class RemoteFile:
    path: str
    sha256: str
    bytes: int


@dataclass(frozen=True)
class Download:
    path: str
    tmp_path: Path
    sha256: str
    bytes: int
    listed_sha256: str


class PortalClient:
    """One persistent connection per thread, so parallel downloads reuse their sockets."""

    def __init__(self, base_url: str, token: str, timeout: float = 60, retries: int = 4):
        parsed = urllib.parse.urlsplit(base_url)
        self._scheme = parsed.scheme
        self._host = parsed.netloc
        self._prefix = parsed.path.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}", "Connection": "keep-alive"}
        self._timeout = timeout
        self._retries = retries
        self._local = threading.local()

    def _conn(self) -> http.client.HTTPConnection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            cls = http.client.HTTPSConnection if self._scheme == "https" else http.client.HTTPConnection
            conn = cls(self._host, timeout=self._timeout)
            self._local.conn = conn
        return conn

    def _reset(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
        self._local.conn = None

    def _get(self, path: str, sink) -> None:
        """GET path and pass the open response to sink(response). Retries transient failures."""
        url = self._prefix + path
        for attempt in range(self._retries + 1):
            try:
                conn = self._conn()
                conn.request("GET", url, headers=self._headers)
                resp = conn.getresponse()
                if resp.status == 200:
                    sink(resp)
                    return
                body = resp.read()
                if resp.status == 401:
                    raise AuthError("portal rejected the token (401)")
                if resp.status not in RETRY_STATUS:
                    raise PortalError(f"GET {path}: HTTP {resp.status}: {body[:200]!r}")
                error: Exception = PortalError(f"GET {path}: HTTP {resp.status}")
            except (http.client.HTTPException, OSError) as exc:
                self._reset()
                error = exc
            if attempt == self._retries:
                raise PortalError(f"GET {path} failed after {attempt + 1} attempts: {error}")
            delay = min(30, 2**attempt) * (0.5 + random.random())
            log.warning("GET %s failed (%s), retrying in %.1fs", path, error, delay)
            time.sleep(delay)

    def list_files(self) -> list[RemoteFile]:
        result: dict = {}
        self._get("/api/v1/files", lambda resp: result.update(json.loads(resp.read())))
        return [RemoteFile(f["path"], f["sha256"], f["bytes"]) for f in result["files"]]

    def download(self, remote: RemoteFile, tmp_dir: Path) -> Download:
        """Stream a file into tmp_dir, hashing while reading, and verify it against the listing.

        Raises IntegrityError (and leaves nothing behind) if the body is truncated or does not hash
        to the listed sha256, so a bad transfer can never be recorded as a new version."""
        quoted = urllib.parse.quote(remote.path, safe="/")
        holder: dict = {}

        def sink(resp):
            digest = hashlib.sha256()
            size = 0
            out = tempfile.NamedTemporaryFile(dir=tmp_dir, delete=False)
            holder["tmp"] = Path(out.name)
            with out:
                while chunk := resp.read(CHUNK):
                    digest.update(chunk)
                    out.write(chunk)
                    size += len(chunk)
                out.flush()
                os.fsync(out.fileno())
            holder.update(sha=digest.hexdigest(), size=size)

        try:
            self._get(f"/api/v1/files/{quoted}", sink)
            if holder["size"] != remote.bytes or holder["sha"] != remote.sha256:
                raise IntegrityError(
                    f"{remote.path}: got {holder['size']} bytes sha {holder['sha'][:12]}, "
                    f"listing says {remote.bytes} bytes sha {remote.sha256[:12]}"
                )
        except BaseException:
            if "tmp" in holder:
                holder["tmp"].unlink(missing_ok=True)
            raise
        return Download(remote.path, holder["tmp"], holder["sha"], holder["size"], remote.sha256)
