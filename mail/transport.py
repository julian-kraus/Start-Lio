"""Network access: IMAP for reading, SMTP for sending. Nothing else in the package opens a socket."""

from __future__ import annotations

import imaplib
import re
import smtplib
import ssl
from dataclasses import dataclass
from datetime import date
from email.message import EmailMessage
from typing import Iterator

SPECIAL_USE = ("\\All", "\\Junk", "\\Sent", "\\Trash", "\\Drafts")
_LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\) (?P<delim>"[^"]*"|NIL) (?P<name>.+)$')
_NUM = {k: re.compile(rb"%s (\d+)" % k.encode()) for k in ("UID", "X-GM-MSGID", "X-GM-THRID")}
FETCH_BATCH = 50


class SendRejected(Exception):
    """The server refused the message; nothing was delivered. Safe to mark failed."""


class SendUnknown(Exception):
    """The connection broke while the message was being handed over. It may or may not have been sent."""


@dataclass(frozen=True)
class RawItem:
    uid: int
    raw: bytes
    gm_msgid: int | None
    gm_thrid: int | None


def _quote(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def parse_list_line(line: bytes) -> tuple[set[str], str] | None:
    m = _LIST_RE.match(line)
    if not m:
        return None
    name = m.group("name").decode("utf-8", "replace")
    if name.startswith('"') and name.endswith('"'):
        name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return set(m.group("flags").decode().split()), name


def parse_fetch(data: list) -> list[RawItem]:
    """imaplib returns tuples (b'<n> (UID .. X-GM-MSGID .. BODY[] {size}', raw) followed by b')'."""
    items = []
    for part in data:
        if not isinstance(part, tuple) or len(part) < 2:
            continue
        head = part[0]
        nums = {k: (int(m.group(1)) if (m := rx.search(head)) else None) for k, rx in _NUM.items()}
        if nums["UID"] is None:
            continue
        items.append(RawItem(nums["UID"], part[1], nums["X-GM-MSGID"], nums["X-GM-THRID"]))
    return items


class Fetcher:
    def __init__(self, host: str, port: int, user: str, password: str, timeout: float = 60):
        self.host, self.port, self.user, self.password, self.timeout = host, port, user, password, timeout
        self.imap: imaplib.IMAP4_SSL | None = None

    def __enter__(self) -> "Fetcher":
        self.imap = imaplib.IMAP4_SSL(self.host, self.port, ssl_context=ssl.create_default_context(),
                                      timeout=self.timeout)
        self.imap.login(self.user, self.password)
        return self

    def __exit__(self, *exc) -> None:
        if self.imap is not None:
            try:
                self.imap.logout()
            except Exception:
                pass
            self.imap = None

    def folders(self) -> dict[str, str]:
        """Special-use flag -> folder name. Gmail localizes names, so flags are the only stable handle."""
        typ, lines = self.imap.list()
        found = {"INBOX": "INBOX"}
        for line in lines or []:
            parsed = parse_list_line(line) if isinstance(line, bytes) else None
            if not parsed:
                continue
            flags, name = parsed
            for flag in SPECIAL_USE:
                if flag in flags:
                    found[flag] = name
        return found

    def select(self, folder: str) -> int:
        typ, _ = self.imap.select(_quote(folder), readonly=True)
        if typ != "OK":
            raise RuntimeError(f"cannot select {folder!r}")
        typ, resp = self.imap.response("UIDVALIDITY")
        return int(resp[0]) if resp and resp[0] else 0

    def _fetch_uids(self, uids: list[int]) -> Iterator[RawItem]:
        for i in range(0, len(uids), FETCH_BATCH):
            chunk = ",".join(str(u) for u in uids[i:i + FETCH_BATCH])
            typ, data = self.imap.uid("FETCH", chunk, "(UID X-GM-MSGID X-GM-THRID BODY.PEEK[])")
            if typ != "OK":
                raise RuntimeError(f"UID FETCH failed: {typ}")
            yield from sorted(parse_fetch(data), key=lambda r: r.uid)

    def search_uids(self, *criteria: str) -> list[int]:
        typ, data = self.imap.uid("SEARCH", *criteria)
        if typ != "OK" or not data or not data[0]:
            return []
        return sorted(int(x) for x in data[0].split())

    def fetch_new(self, last_uid: int | None, since: date) -> Iterator[RawItem]:
        if last_uid is None:
            uids = self.search_uids("SINCE", since.strftime("%d-%b-%Y"))
        else:
            # "n:*" always returns the newest message even when it is <= n, hence the filter.
            uids = [u for u in self.search_uids("UID", f"{last_uid + 1}:*") if u > last_uid]
        yield from self._fetch_uids(uids)

    def find_message_id(self, message_id: str) -> RawItem | None:
        mid = message_id.strip("<>").replace('"', "")
        uids = self.search_uids("X-GM-RAW", _quote(f"rfc822msgid:{mid}"))
        items = list(self._fetch_uids(uids[-1:])) if uids else []
        return items[0] if items else None


class Sender:
    def __init__(self, host: str, port: int, user: str, password: str, timeout: float = 60):
        self.host, self.port, self.user, self.password, self.timeout = host, port, user, password, timeout

    def _connect(self) -> smtplib.SMTP:
        ctx = ssl.create_default_context()
        if self.port == 465:
            smtp = smtplib.SMTP_SSL(self.host, self.port, context=ctx, timeout=self.timeout)
        else:
            smtp = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
            smtp.starttls(context=ctx)
        smtp.login(self.user, self.password)
        return smtp

    def check(self) -> None:
        smtp = self._connect()
        smtp.quit()

    def send(self, msg: EmailMessage) -> str:
        try:
            smtp = self._connect()
        except Exception as exc:          # nothing handed over yet
            raise SendRejected(f"connect/login failed: {type(exc).__name__}: {exc}") from exc
        try:
            refused = smtp.send_message(msg)
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError) as exc:
            raise SendRejected(f"{type(exc).__name__}: {exc}") from exc
        except Exception as exc:
            raise SendUnknown(f"{type(exc).__name__}: {exc}") from exc
        finally:
            try:
                smtp.quit()
            except Exception:
                pass
        if refused:
            raise SendUnknown(f"some recipients refused: {sorted(refused)}")
        return "accepted"
