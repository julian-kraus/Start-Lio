"""Raw RFC 822 bytes -> ParsedMessage. Pure functions: no DB, no network."""

from __future__ import annotations

import email
import email.policy
import email.utils
import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage, Message
from html.parser import HTMLParser

AUTO_HEADERS = ("x-autoreply", "x-autorespond", "x-auto-response-suppress")
INSTRUCTION_LIKE = re.compile(
    r"ignore (all |any )?(previous|prior|above) (instructions|messages)|you are (an? )?(ai|assistant|language model)"
    r"|system prompt|disregard (the|your) (instructions|rules)|forward (this|all) (mail|email|messages) to",
    re.I)
POLISH = set("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ")


@dataclass
class Part:
    filename: str
    content_type: str
    data: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


@dataclass
class BounceInfo:
    failed_recipients: list[str]
    original_message_id: str | None
    status: str | None


@dataclass
class ParsedMessage:
    message_id: str
    raw_sha256: str
    in_reply_to: str | None
    references: list[str]
    from_addr: str
    from_name: str
    to_addrs: list[str]
    cc_addrs: list[str]
    subject: str
    date: datetime | None
    body_text: str
    attachments: list[Part] = field(default_factory=list)
    kind_hint: str | None = None          # bounce | auto_reply | None
    bounce: BounceInfo | None = None
    lang: str | None = None
    flags: list[str] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)


def normalize_addr(addr: str | None) -> str:
    return (addr or "").strip().strip("<>").strip().lower()


def _msgids(value: str | None) -> list[str]:
    return re.findall(r"<[^<>\s]+>", value or "")


def extract_tag(subject: str, prefix: str) -> str | None:
    m = re.search(r"\[(%s-[0-9A-Z]{4})\]" % re.escape(prefix), subject or "", re.I)
    return m.group(1).upper() if m else None


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "head", "title"}
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "table", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_to_text(html: str) -> str:
    p = _TextExtractor()
    try:
        p.feed(html)
        p.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)
    text = "".join(p.out)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


def detect_lang(text: str) -> str | None:
    """Script heuristic: Hangul -> ko, Han -> zh, Polish diacritics -> pl, Latin -> en. Approximate by design."""
    sample = text[:4000]
    counts = {"ko": 0, "zh": 0, "latin": 0}
    pl = 0
    for ch in sample:
        if ch in POLISH:
            pl += 1
        if not ch.isalpha():
            continue
        name = unicodedata.name(ch, "")
        if name.startswith("HANGUL"):
            counts["ko"] += 1
        elif name.startswith("CJK UNIFIED") or name.startswith("CJK COMPATIBILITY"):
            counts["zh"] += 1
        elif "LATIN" in name:
            counts["latin"] += 1
    if counts["ko"] >= 3 and counts["ko"] >= counts["zh"]:
        return "ko"
    if counts["zh"] >= 3:
        return "zh"
    if counts["latin"] >= 3:
        return "pl" if pl >= 2 else "en"
    return None


def safe_filename(name: str | None, fallback: str) -> str:
    name = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable() and ch not in '<>:"|?*').strip(" .")
    if not name:
        name = fallback
    if len(name) > 120:
        stem, dot, ext = name.rpartition(".")
        name = (stem[:110] + "." + ext[:8]) if dot and len(ext) <= 8 else name[:120]
    return name


def _decode_part(part: Message) -> str:
    try:
        return part.get_content()
    except Exception:
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:
            return payload.decode("utf-8", errors="replace")


def _header(msg: Message, name: str) -> str:
    try:
        value = msg.get(name)
    except Exception:
        return ""
    return str(value) if value is not None else ""


def _addresses(msg: Message, name: str) -> list[tuple[str, str]]:
    raw = msg.get_all(name, []) or []
    try:
        pairs = email.utils.getaddresses([str(v) for v in raw])
    except Exception:
        pairs = []
    return [(n, normalize_addr(a)) for n, a in pairs if a]


def _bounce(msg: Message) -> BounceInfo | None:
    failed, original, status = [], None, None
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype == "message/delivery-status":
            payload = part.get_payload()
            blocks = payload if isinstance(payload, list) else []
            text = "\n".join(str(b) for b in blocks) if blocks else _decode_part(part)
            failed += [normalize_addr(m.split(";")[-1]) for m in re.findall(r"(?im)^Final-Recipient:\s*(.+)$", text)]
            if (m := re.search(r"(?im)^Status:\s*(\S+)", text)):
                status = m.group(1)
        elif ctype in ("message/rfc822", "text/rfc822-headers") and original is None:
            inner = part.get_payload()
            if isinstance(inner, list) and inner:
                original = (_msgids(_header(inner[0], "Message-ID")) or [None])[0]
            else:
                text = inner if isinstance(inner, str) else _decode_part(part)
                if (m := re.search(r"(?im)^Message-ID:\s*(<[^>]+>)", text)):
                    original = m.group(1)
    for h in ("X-Failed-Recipients",):
        failed += [normalize_addr(a) for a in _header(msg, h).split(",") if a.strip()]
    failed = sorted(set(a for a in failed if a))
    return BounceInfo(failed, original, status)


def _is_auto_reply(msg: Message) -> bool:
    auto = _header(msg, "Auto-Submitted").strip().lower()
    if auto and auto != "no":
        return True
    if any(_header(msg, h) for h in AUTO_HEADERS):
        return True
    return _header(msg, "Precedence").strip().lower() in ("auto_reply", "bulk", "junk")


def parse(raw: bytes) -> ParsedMessage:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    raw_sha = hashlib.sha256(raw).hexdigest()
    mid = (_msgids(_header(msg, "Message-ID")) or [f"<sha256.{raw_sha}@mail.local>"])[0]
    from_pairs = _addresses(msg, "From")
    from_name, from_addr = from_pairs[0] if from_pairs else ("", "")
    try:
        date = email.utils.parsedate_to_datetime(_header(msg, "Date")) if _header(msg, "Date") else None
    except (TypeError, ValueError):
        date = None

    plain, html, attachments = [], [], []
    report = msg.get_content_type() == "multipart/report"
    for i, part in enumerate(msg.walk()):
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        disposition = (part.get_content_disposition() or "").lower()
        filename = part.get_filename()
        if report and ctype in ("message/delivery-status", "text/rfc822-headers"):
            continue
        if disposition == "attachment" or filename or ctype not in ("text/plain", "text/html"):
            if ctype.startswith("multipart/") or ctype == "message/delivery-status":
                continue
            try:
                data = part.get_payload(decode=True)
                if data is None:
                    data = part.as_bytes()
            except Exception:
                data = b""
            ext = {"application/pdf": ".pdf", "text/plain": ".txt", "text/html": ".html"}.get(ctype, ".bin")
            attachments.append(Part(safe_filename(filename, f"part{i}{ext}"), ctype, data))
        elif ctype == "text/plain":
            plain.append(_decode_part(part))
        else:
            html.append(_decode_part(part))
    body = "\n\n".join(t.strip() for t in plain if t.strip()) or html_to_text("\n".join(html))

    pm = ParsedMessage(
        message_id=mid, raw_sha256=raw_sha,
        in_reply_to=(_msgids(_header(msg, "In-Reply-To")) or [None])[0],
        references=_msgids(_header(msg, "References")),
        from_addr=from_addr, from_name=from_name,
        to_addrs=[a for _, a in _addresses(msg, "To")], cc_addrs=[a for _, a in _addresses(msg, "Cc")],
        subject=_header(msg, "Subject").replace("\r", "").replace("\n", " ").strip(),
        date=date, body_text=body, attachments=attachments,
        headers={h: _header(msg, h) for h in ("Auto-Submitted", "Precedence", "X-Mail-Draft-ID") if _header(msg, h)},
    )
    sender_local = from_addr.split("@")[0]
    if report or sender_local in ("mailer-daemon", "postmaster"):
        pm.kind_hint, pm.bounce = "bounce", _bounce(msg)
    elif _is_auto_reply(msg):
        pm.kind_hint = "auto_reply"
    pm.lang = detect_lang(body)
    if INSTRUCTION_LIKE.search(body) or INSTRUCTION_LIKE.search(pm.subject):
        pm.flags.append("instruction_like_text")
    return pm
