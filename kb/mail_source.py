"""Supplier email as a kb input: follows mail.events (document_added) with a cursor in mail.consumer_cursors.

Bodies and attachments go through ingest.process_file. For every kb command, what a sender wrote stays
marked as theirs:
- every fact carries locator.mail (trust, scenario, sent_at, ...), taken from the mail event, never from the text;
- mail never yields a `supplier` master data fact, because mail.contacts builds the recipient allowlist from those;
- fact ids of mail are separate from those of a portal file with the same bytes;
- mail never adds to the shared registry: no doc kinds, schema maps or fact types.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path, PurePosixPath

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from tracker.config import PROJECT_ROOT
from tracker.diff import detect_kind
from tracker.store import SHA_RE, check_path

from . import facts as facts_mod
from .config import SCHEMA_NAME
from .ingest import MAIL_PREFIX as PREFIX, is_mail, process_file
from .llm.base import LLMClient, StructureProposal
from .tabular import TABULAR_CONTAINERS

log = logging.getLogger("kb")
CONSUMER = "kb-mail"
CHANNEL = "mail_events"
TRUSTED = ("supplier_contact", "test")
OWN_KEY = {"offer": "offer_ref", "supplier_statement": "statement_no"}
META_KEYS = ("trust", "thread_id", "message_id", "supplier_id", "persona_id")
MASTER_TYPE = "supplier"
CLAIMED_TYPE = f"x_mail_{MASTER_TYPE}"
NO_KIND = "no known doc kind fits, and mail cannot add one"
# Event ids are taken before commit, so a lower id can become visible after the cursor has passed it.
LOOKBACK = 200


def second_path(path: str, sha: str) -> str:
    """Where a document lives when another part of the same email already has its path. A filename cannot contain
    a slash, so no attachment name can produce this path."""
    p = PurePosixPath(path)
    return f"{p.parent}/{sha[:16]}/{p.name}"


def label(payload: dict) -> dict:
    meta = {**{k: payload.get(k) for k in META_KEYS}, "scenario": payload["trust"] == "test"}
    # When the mail was received. Without that there is no time: the load time would not be the sender's.
    return {**meta, "sent_at": payload["received_at"]} if payload.get("received_at") else meta


def last_fact_event(conn) -> int:
    return conn.execute("SELECT coalesce(max(id), 0) AS id FROM fact_events").fetchone()["id"]


def announce_stored_types(conn, path: str, since: int) -> None:
    """process_file writes its event before the facts are relabelled. Call in that same transaction, so that a
    reader of the feed only ever sees the types that were stored."""
    conn.execute("UPDATE fact_events e SET fact_types = ARRAY(SELECT DISTINCT fact_type FROM facts "
                 "WHERE source_path = e.path AND valid ORDER BY 1) WHERE e.path = %s AND e.id > %s", (path, since))


def park_unrouted(conn) -> list[dict]:
    """Classified mail that fits no kind would stay pending: classified on every poll and ahead of portal files."""
    return conn.execute(
        "UPDATE extraction_queue SET state = 'failed', last_error = %s, updated_at = now() "
        "WHERE state = 'pending' AND starts_with(path, %s) AND routing ? 'rules' RETURNING path",
        (NO_KIND, PREFIX)).fetchall()


def release_copies(conn, path: str | None = None) -> None:
    """A portal file can reuse the facts of identical bytes seen in an email. It is not mail: drop the label."""
    conn.execute(
        "UPDATE facts SET locator = locator - 'mail', "
        "fact_type = CASE WHEN fact_type = %s THEN %s ELSE fact_type END "
        "WHERE NOT starts_with(source_path, %s) AND locator ? 'mail' AND (%s::text IS NULL OR source_path = %s)",
        (CLAIMED_TYPE, MASTER_TYPE, PREFIX, path, path))


class MailSource:
    def __init__(self, schema: str, data_dir: Path):
        if not SCHEMA_NAME.match(schema):
            raise SystemExit(f"invalid MAIL_SCHEMA {schema!r}")
        self.schema = schema
        self.objects = Path(data_dir) / "objects"

    @classmethod
    def from_env(cls) -> MailSource:
        return cls(os.environ.get("MAIL_SCHEMA", "mail"),
                   Path(os.environ.get("MAIL_DATA_DIR", PROJECT_ROOT / "mail_data")))

    def _sql(self, query: str) -> sql.Composed:
        return sql.SQL(query).format(mail=sql.Identifier(self.schema))

    def _blob(self, sha: str) -> Path:
        # Not a BlobStore: that creates directories, and kb mounts the mail directory read-only.
        if not SHA_RE.match(sha):
            raise ValueError(f"not a sha256: {sha!r}")
        return self.objects / sha[:2] / sha

    def read(self, sha: str) -> bytes:
        data = self._blob(sha).read_bytes()
        if hashlib.sha256(data).hexdigest() != sha:
            raise ValueError(f"mail blob {sha} does not match its hash")
        return data

    def available(self, conn) -> bool:
        return conn.execute("SELECT to_regclass(%s) IS NOT NULL AND to_regclass(%s) IS NOT NULL AS ok",
                            (f"{self.schema}.events", f"{self.schema}.consumer_cursors")).fetchone()["ok"]

    def catch_up(self, conn, structured: Path, confidence_map: dict) -> list[dict]:
        """Process new mail documents. Each commits together with the cursor, so a crash repeats at most one."""
        if not self.available(conn):
            conn.commit()
            return []
        conn.execute(self._sql("INSERT INTO {mail}.consumer_cursors (consumer, last_event_id) VALUES (%s, 0) "
                               "ON CONFLICT DO NOTHING"), (CONSUMER,))
        cursor = conn.execute(self._sql("SELECT last_event_id FROM {mail}.consumer_cursors WHERE consumer = %s"),
                              (CONSUMER,)).fetchone()["last_event_id"]
        # A blob that was not there yet is tried again once it is, however old its event is.
        retry = [r["event_id"] for r in conn.execute(
            "SELECT sha256, (detail->>'retry_event')::bigint AS event_id FROM documents "
            "WHERE active AND status = 'failed' AND starts_with(path, %s) AND detail ? 'retry_event'", (PREFIX,))
            if self._blob(r["sha256"]).is_file()]
        pending = conn.execute(self._sql(
            """SELECT e.id, e.payload FROM {mail}.events e
               WHERE e.kind = 'document_added' AND (e.id = ANY(%s::bigint[]) OR (e.id > %s AND NOT EXISTS (
                 SELECT 1 FROM documents d WHERE d.sha256 = e.payload->>'sha256' AND d.path IN (
                   e.payload->>'path', regexp_replace(e.payload->>'path', '([^/]+)$',
                                                      left(e.payload->>'sha256', 16) || '/\\1')))))
               ORDER BY e.id"""), (retry, max(0, cursor - LOOKBACK))).fetchall()
        conn.commit()
        done = []
        for event in pending:
            result = self._handle(conn, event, structured, confidence_map, first_time=event["id"] > cursor)
            if result:
                done.append(result)
            conn.execute(self._sql("UPDATE {mail}.consumer_cursors SET last_event_id = GREATEST(last_event_id, %s), "
                                   "updated_at = now() WHERE consumer = %s"), (event["id"], CONSUMER))
            conn.commit()
        return done

    def _handle(self, conn, event: dict, structured: Path, confidence_map: dict, *,
                first_time: bool) -> dict | None:
        payload = event["payload"]
        path, sha = str(payload.get("path") or ""), str(payload.get("sha256") or "")
        try:
            check_path(path)
            valid = is_mail(path) and SHA_RE.match(sha) and payload.get("trust") in TRUSTED
        except ValueError:
            valid = False
        if not valid:
            if first_time:
                log.warning("mail event %s ignored: bad path, hash or sender trust", event["id"])
            return None
        # pending was read once, so the same part can be in it twice. A part whose blob was missing is not done.
        if conn.execute("SELECT 1 FROM documents WHERE sha256 = %s AND path = ANY(%s) AND NOT detail ? 'retry_event'",
                        (sha, [path, second_path(path, sha)])).fetchone():
            return None
        taken = conn.execute("SELECT 1 FROM documents WHERE path = %s AND sha256 <> %s AND active",
                             (path, sha)).fetchone()
        if taken:
            path = second_path(path, sha)
        try:
            status = self._process(conn, path, sha, self.read(sha), label(payload), structured, confidence_map)
        except (psycopg.OperationalError, psycopg.InterfaceError):
            raise   # not the document's fault: the worker reconnects and the cursor has not moved
        except Exception as exc:
            conn.rollback()
            status, detail = "failed", {"error": f"{type(exc).__name__}: {exc}"[:2000]}
            if isinstance(exc, FileNotFoundError):
                detail["retry_event"] = event["id"]
            facts_mod.upsert_document(conn, path, sha, container=None, status=status, detail=detail)
            log.warning("mail event %s (%s) failed: %s", event["id"], path, type(exc).__name__)
        return {"path": path, "event": "document_added", "status": status}

    def _process(self, conn, path: str, sha: str, data: bytes, meta: dict, structured: Path,
                 confidence_map: dict) -> str:
        container = detect_kind(path, data)
        tabular = container in TABULAR_CONTAINERS
        # A table's fact ids ignore the path, so parsing would rewrite the facts another path has for the same
        # bytes. Those step aside until the email's facts have ids of their own (all in this transaction).
        others = conn.execute(
            "UPDATE facts SET fact_id = 'held:' || fact_id WHERE source_sha256 = %s AND source_path <> %s "
            "RETURNING fact_id, valid", (sha, path)).fetchall() if tabular else []
        since = last_fact_event(conn)
        # No envelope lookup for mail: an envelope names a portal path and would make that path current again.
        # No LLM here: process_file would only use it to let the document design a schema map.
        status = process_file(conn, path, sha, data, structured / "no-mail-envelopes", confidence_map)
        conn.execute(
            """UPDATE facts SET
                 locator = CASE jsonb_typeof(locator) WHEN 'object' THEN locator ELSE '{}'::jsonb END
                           || jsonb_build_object('mail', %s::jsonb),
                 fact_type = CASE WHEN fact_type = %s THEN %s ELSE fact_type END,
                 fact_id = CASE WHEN %s THEN left(encode(sha256(convert_to(fact_id || '|' || source_path, 'UTF8')),
                                                         'hex'), 32) ELSE fact_id END
               WHERE source_path = %s AND source_sha256 = %s""",
            (Jsonb(meta), MASTER_TYPE, CLAIMED_TYPE, tabular, path, sha))
        announce_stored_types(conn, path, since)
        if others:
            conn.execute("UPDATE facts SET fact_id = substr(fact_id, 6), valid = fact_id = ANY(%s) "
                         "WHERE fact_id = ANY(%s)",
                         ([f["fact_id"] for f in others if f["valid"]], [f["fact_id"] for f in others]))
        return status

    def label_of(self, conn, path: str, sha: str) -> dict:
        rows = conn.execute(self._sql("SELECT payload FROM {mail}.events WHERE kind = 'document_added' "
                                      "AND payload->>'sha256' = %s ORDER BY id"), (sha,))
        for row in rows:
            original = row["payload"]["path"]
            if path in (original, second_path(original, sha)) and row["payload"].get("trust") in TRUSTED:
                return label(row["payload"])
        raise LookupError(f"no mail event for {path}")


class LabellingClient(LLMClient):
    """Puts the mail label on extracted facts before they get their id, so no fact is ever stored without it."""

    def __init__(self, inner: LLMClient, source: MailSource, conn):
        self.inner, self.source, self.conn = inner, source, conn
        self.name, self.model, self.needs_images = inner.name, inner.model, inner.needs_images

    @property
    def label(self) -> str:
        return self.inner.label

    def supports(self, role: str) -> bool:
        return self.inner.supports(role)

    def classify(self, document, kinds):
        return self.inner.classify(document, kinds)

    def design(self, document, similar, contract, sample_rows=None):
        if is_mail(document.path):
            return StructureProposal("none", "", NO_KIND)
        return self.inner.design(document, similar, contract, sample_rows)

    def extract(self, document, structure, schema, contract):
        env = self.inner.extract(document, structure, schema, contract)
        meta = self.source.label_of(self.conn, document.path, document.sha256) if is_mail(document.path) else None
        if meta is not None:
            env["proposed_fact_types"] = []
        for fact in env.get("facts") or []:
            locator = fact.get("locator")
            if meta is None:
                if isinstance(locator, dict):
                    locator.pop("mail", None)   # only a mail event can give a document a sender
                continue
            if not isinstance(locator, dict):
                locator = {} if locator is None else {"at": locator}
            fact["locator"] = {**locator, "mail": meta}
            if fact.get("fact_type") == MASTER_TYPE:
                fact["fact_type"] = CLAIMED_TYPE
            own_key = OWN_KEY.get(fact.get("fact_type"))
            if own_key and isinstance(fact.get("subject"), dict):     # who wrote is the envelope's, not the model's
                sender = meta.get("supplier_id") or meta.get("persona_id")
                if sender:
                    fact["subject"]["supplier_id"] = sender
                if meta.get("message_id") and not fact["subject"].get(own_key):
                    fact["subject"][own_key] = meta["message_id"]
        return env
