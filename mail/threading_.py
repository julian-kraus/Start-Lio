"""Match an inbound message to a thread. Pure logic over a Lookups interface (DB-backed or fake in tests)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .parse import ParsedMessage, extract_tag


@dataclass(frozen=True)
class Contact:
    address: str
    supplier_id: str | None
    is_test: bool
    language: str | None = None


@dataclass(frozen=True)
class Match:
    thread_id: str | None
    method: str            # in_reply_to | references | gm_thrid | subject_tag | supplier_single_open | new | none | self
    supplier_id: str | None
    trust: str             # supplier_contact | test | unknown_sender | self


class Lookups(Protocol):
    def thread_by_message_ids(self, message_ids: list[str]) -> str | None: ...
    def thread_by_gm_thrid(self, gm_thrid: int) -> str | None: ...
    def thread_by_tag(self, tag: str) -> str | None: ...
    def thread_supplier(self, thread_id: str) -> str | None: ...
    def contact(self, address: str) -> Contact | None: ...
    def waiting_threads_for(self, supplier_key: str) -> list[str]: ...


def supplier_key(contact: Contact) -> str:
    """Test contacts have no supplier id; their threads are keyed by address instead."""
    return contact.supplier_id or f"test:{contact.address}"


def match(p: ParsedMessage, gm_thrid: int | None, look: Lookups, own_address: str, tag_prefix: str) -> Match:
    own = p.from_addr == own_address
    contact = None if own else look.contact(p.from_addr)
    if own:
        trust, supplier = "self", None
    elif contact is None:
        trust, supplier = "unknown_sender", None
    else:
        trust, supplier = ("test" if contact.is_test else "supplier_contact"), contact.supplier_id

    def hit(thread_id: str, method: str) -> Match:
        # Our own sent copies take the supplier from the thread; inbound mail keeps the sender's supplier.
        return Match(thread_id, method, look.thread_supplier(thread_id) if trust == "self" else supplier, trust)

    # Unknown senders never get attached to a thread automatically: a human links them.
    if trust == "unknown_sender" and p.kind_hint != "bounce":
        return Match(None, "none", None, trust)

    if p.in_reply_to and (t := look.thread_by_message_ids([p.in_reply_to])):
        return hit(t, "in_reply_to")
    if p.references and (t := look.thread_by_message_ids(p.references)):
        return hit(t, "references")
    if p.kind_hint == "bounce" and p.bounce and p.bounce.original_message_id and \
            (t := look.thread_by_message_ids([p.bounce.original_message_id])):
        return Match(t, "references", look.thread_supplier(t), "self" if own else trust)
    if gm_thrid is not None and (t := look.thread_by_gm_thrid(gm_thrid)):
        return hit(t, "gm_thrid")
    tag = extract_tag(p.subject, tag_prefix)
    if tag and (t := look.thread_by_tag(tag)):
        return hit(t, "subject_tag")
    if trust == "unknown_sender":
        return Match(None, "none", None, trust)
    if trust == "self":
        return Match(None, "self", None, trust)
    waiting = look.waiting_threads_for(supplier_key(contact))
    if len(waiting) == 1:
        return Match(waiting[0], "supplier_single_open", supplier, trust)
    return Match(None, "new", supplier, trust)
