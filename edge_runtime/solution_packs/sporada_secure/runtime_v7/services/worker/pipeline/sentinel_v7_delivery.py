"""Sentinel V7 per-deployment transactional delivery.

Camera processes remain isolated, but all processes in one logical deployment
share this SQLite outbox.  SQLite is therefore the authority for sequence
allocation, dependency ordering, upload claims, stream identity, watermarks,
and crash-safe skip markers.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import random
import re
import sqlite3
import ssl
import threading
import time
from typing import Any, Iterator
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, Request, build_opener
import uuid

import jsonschema

from .structured_logging import sanitize_text


KINDS = ("observation", "evidence", "embedding")
TERMINAL_STATES = ("acked", "skipped", "resolved_by_remote_receipt")
SKIP_REASONS = {
    "validation_failed", "evidence_expired", "object_unreadable",
    "outbox_overflow", "retention_expired",
}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class RetryLater(RuntimeError):
    def __init__(self, message: str, *, kind: str = "unknown",
                 status: int | None = None, reason: str | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.reason = reason or message


class ProtocolError(RuntimeError):
    pass


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def request_json_bytes(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(value))[:160] or "deployment"


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class SentinelV7Metrics:
    """Deployment delivery metrics exposed through the existing metrics scan."""

    def __init__(self, state_root: Path, deployment_id: str, camera_id: str) -> None:
        self.path = state_root / "metrics" / (
            f"sentinel-delivery-{_safe_name(camera_id)}.json"
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.deployment_id = deployment_id
        self.camera_id = camera_id
        self.lock = threading.Lock()
        self.counters = {
            kind: {
                "upload_attempts_total": 0,
                "acknowledgements_total": 0,
                "retries_total": 0,
                "http_rejections": {},
                "retry_reasons": {},
            } for kind in KINDS
        }
        self.skips_total = 0
        self.bootstrap_failures_total = 0

    def attempt(self, kind: str) -> None:
        self._increment(kind, "upload_attempts_total")

    def acknowledged(self, kind: str) -> None:
        self._increment(kind, "acknowledgements_total")

    def retry(self, kind: str, reason: str, status: int | None = None) -> None:
        with self.lock:
            item = self.counters.setdefault(kind, {})
            item["retries_total"] = int(item.get("retries_total", 0)) + 1
            key = str(status) if status is not None else str(reason)[:120]
            reasons = item.setdefault("retry_reasons", {})
            reasons[key] = int(reasons.get(key, 0)) + 1

    def rejected(self, kind: str, status: int, reason: str) -> None:
        with self.lock:
            values = self.counters.setdefault(kind, {}).setdefault("http_rejections", {})
            key = f"{status}:{str(reason)[:80]}"
            values[key] = int(values.get(key, 0)) + 1

    def snapshot(self, store: "SentinelV7Outbox") -> None:
        summary = store.metrics_summary(camera_id=self.camera_id)
        with self.lock:
            payload = {
                "schema_version": "2.0",
                "schema_scope": "delivery_telemetry",
                "wire_schema_version": "7.0",
                "contract": "sentry-v7",
                "deployment_id": self.deployment_id,
                "camera_id": self.camera_id,
                "observed_at": time.time(),
                "artifacts": {
                    kind: {**self.counters.get(kind, {}), **summary["artifacts"][kind]}
                    for kind in KINDS
                },
                "outbox_records": summary["outbox_records"],
                "outbox_bytes": summary["outbox_bytes"],
                "highest_contiguous_sequence": summary["highest_contiguous_sequence"],
                "highest_received_sequence": summary["highest_received_sequence"],
                "stream_id": summary["stream_id"],
                "skip_pending": summary["skip_pending"],
                "skips_total": self.skips_total,
                "bootstrap_failures_total": self.bootstrap_failures_total,
            }
            temporary = self.path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(payload, separators=(",", ":"), allow_nan=False),
                encoding="utf-8",
            )
            os.replace(temporary, self.path)

    def _increment(self, kind: str, field: str) -> None:
        with self.lock:
            item = self.counters.setdefault(kind, {})
            item[field] = int(item.get(field, 0)) + 1


class SentinelV7Outbox:
    """Multi-process-safe V7 outbox for exactly one deployment."""

    def __init__(self, state_root: Path, deployment_id: str, camera_id: str) -> None:
        self.deployment_id = str(deployment_id)
        self.camera_id = str(camera_id)
        self.root = state_root / "sentinel_v7"
        self.evidence = self.root / "evidence"
        self.dead_letter = state_root / "dead-letter"
        self.database = self.root / "outbox.sqlite"
        self.root.mkdir(parents=True, exist_ok=True)
        self.evidence.mkdir(parents=True, exist_ok=True)
        self.dead_letter.mkdir(parents=True, exist_ok=True)
        self.metrics = SentinelV7Metrics(
            state_root, self.deployment_id, self.camera_id
        )
        schema_dir = Path(__file__).resolve().parents[3] / "image_schema"
        self.validators = {
            kind: jsonschema.Draft202012Validator(
                json.loads((schema_dir / f"{kind}.schema.json").read_text(encoding="utf-8")),
                format_checker=jsonschema.FormatChecker(),
            ) for kind in ("observation", "evidence", "embedding", "acknowledgement")
        }
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS records (
                    sequence INTEGER PRIMARY KEY,
                    package_id TEXT NOT NULL,
                    camera_id TEXT NOT NULL,
                    object_type TEXT NOT NULL CHECK(object_type IN ('observation','evidence','embedding')),
                    object_id TEXT NOT NULL UNIQUE,
                    parent_sequence INTEGER REFERENCES records(sequence),
                    payload_json BLOB NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    content_sha256 TEXT,
                    evidence_path TEXT,
                    state TEXT NOT NULL CHECK(state IN (
                        'pending','acked','dead_letter','skip_pending','skipped',
                        'resolved_by_remote_receipt')),
                    skip_reason TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT,
                    outbox_created_at REAL NOT NULL,
                    skipped_at REAL,
                    claimed_by TEXT,
                    claimed_until REAL
                );
                CREATE INDEX IF NOT EXISTS records_pending
                    ON records(state, next_attempt_at, sequence);
                CREATE INDEX IF NOT EXISTS records_package
                    ON records(package_id, sequence);
                CREATE TABLE IF NOT EXISTS dead_letters (
                    sequence INTEGER PRIMARY KEY,
                    object_type TEXT NOT NULL,
                    object_id TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    last_error TEXT,
                    attempts INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    skipped_at REAL
                );
            """)
            existing = db.execute(
                "SELECT value FROM metadata WHERE key='deployment_id'"
            ).fetchone()
            if existing is not None and existing[0] != self.deployment_id:
                raise ValueError("V7 outbox belongs to a different deployment_id")
            db.execute(
                "INSERT OR IGNORE INTO metadata(key,value) VALUES('deployment_id',?)",
                (self.deployment_id,),
            )
            db.execute("INSERT OR IGNORE INTO metadata(key,value) VALUES('next_sequence','1')")
            db.execute("INSERT OR IGNORE INTO metadata(key,value) VALUES('highest_contiguous_sequence','0')")
            db.execute("INSERT OR IGNORE INTO metadata(key,value) VALUES('highest_received_sequence','0')")

    def persist(
        self,
        observation: dict[str, Any],
        evidence: list[tuple[dict[str, Any], bytes]],
        embeddings: list[dict[str, Any]] | None = None,
        scenes: list[dict[str, Any]] | None = None,
    ) -> Path:
        del scenes
        observation_id = str(observation["observation_id"])
        if observation.get("deployment_id") != self.deployment_id:
            raise ValueError("observation deployment_id does not match V7 outbox")
        self.validators["observation"].validate(observation)
        evidence_ids: set[str] = set()
        prepared_evidence: list[tuple[dict[str, Any], bytes, Path, str]] = []
        for metadata, body in evidence:
            self.validators["evidence"].validate(metadata)
            if metadata["observation_id"] != observation_id:
                raise ValueError("evidence parent does not match observation")
            evidence_id = str(metadata["evidence_id"])
            evidence_ids.add(evidence_id)
            content_digest = "sha256:" + hashlib.sha256(body).hexdigest()
            if metadata.get("sha256") != content_digest or metadata.get("size_bytes") != len(body):
                raise ValueError("evidence metadata does not match exact bytes")
            suffix = {"image/png": ".png", "image/jpeg": ".jpg"}[metadata["content_type"]]
            prepared_evidence.append(
                (metadata, body, self.evidence / f"{evidence_id}{suffix}", content_digest)
            )
        embeddings = list(embeddings or [])
        embedding_ids = {str(item["embedding_id"]) for item in embeddings}
        if set(observation.get("evidence_ids") or []) != evidence_ids:
            raise ValueError("observation evidence_ids do not match stored assets")
        if set(observation.get("embedding_ids") or []) != embedding_ids:
            raise ValueError("observation embedding_ids do not match stored embeddings")
        for embedding in embeddings:
            self.validators["embedding"].validate(embedding)
            if embedding["observation_id"] != observation_id:
                raise ValueError("embedding parent does not match observation")
            if embedding["evidence_id"] not in evidence_ids:
                raise ValueError("embedding references missing evidence")

        written: list[Path] = []
        try:
            for _, body, path, _ in prepared_evidence:
                temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
                with temporary.open("wb") as handle:
                    handle.write(body)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
                written.append(path)
            if written:
                _fsync_directory(self.evidence)

            now = time.time()
            with self.transaction() as db:
                sequence = int(self._meta(db, "next_sequence", "1"))
                observation_body = request_json_bytes(observation)
                observation_sequence = sequence
                db.execute(
                    """INSERT INTO records(sequence,package_id,camera_id,object_type,object_id,
                       parent_sequence,payload_json,payload_sha256,state,outbox_created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (sequence, observation_id, self.camera_id, "observation", observation_id,
                     None, observation_body, hashlib.sha256(observation_body).hexdigest(),
                     "pending", now),
                )
                sequence += 1
                evidence_sequences: dict[str, int] = {}
                for metadata, body, path, content_digest in prepared_evidence:
                    metadata_bytes = canonical_json_bytes(metadata)
                    delivery_digest = hashlib.sha256(metadata_bytes + b"\0" + body).hexdigest()
                    evidence_id = str(metadata["evidence_id"])
                    evidence_sequences[evidence_id] = sequence
                    db.execute(
                        """INSERT INTO records(sequence,package_id,camera_id,object_type,object_id,
                           parent_sequence,payload_json,payload_sha256,content_sha256,evidence_path,
                           state,outbox_created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (sequence, observation_id, self.camera_id, "evidence", evidence_id,
                         observation_sequence, metadata_bytes, delivery_digest, content_digest,
                         str(path), "pending", now),
                    )
                    sequence += 1
                for embedding in embeddings:
                    body = request_json_bytes(embedding)
                    db.execute(
                        """INSERT INTO records(sequence,package_id,camera_id,object_type,object_id,
                           parent_sequence,payload_json,payload_sha256,state,outbox_created_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (sequence, observation_id, self.camera_id, "embedding",
                         str(embedding["embedding_id"]),
                         evidence_sequences[str(embedding["evidence_id"])], body,
                         hashlib.sha256(body).hexdigest(), "pending", now),
                    )
                    sequence += 1
                self._set_meta(db, "next_sequence", str(sequence))
            self.metrics.snapshot(self)
            return self.database
        except Exception:
            for path in written:
                path.unlink(missing_ok=True)
            raise

    def claim(self, owner: str, *, limit: int = 8, lease_seconds: float = 45.0) -> dict[str, Any] | None:
        now = time.time()
        with self.transaction() as db:
            db.execute(
                "UPDATE records SET claimed_by=NULL,claimed_until=NULL "
                "WHERE claimed_until IS NOT NULL AND claimed_until<?",
                (now,),
            )
            active = int(db.execute(
                "SELECT COUNT(*) FROM records WHERE claimed_until>=?", (now,)
            ).fetchone()[0])
            if active >= max(1, int(limit)):
                return None
            row = db.execute(
                """SELECT child.* FROM records child
                   LEFT JOIN records parent ON parent.sequence=child.parent_sequence
                   WHERE child.state IN ('pending','skip_pending')
                     AND child.next_attempt_at<=?
                     AND child.claimed_until IS NULL
                     AND (child.state='skip_pending' OR child.parent_sequence IS NULL
                          OR parent.state IN ('acked','skipped','resolved_by_remote_receipt'))
                   ORDER BY child.sequence LIMIT 1""",
                (now,),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE records SET claimed_by=?,claimed_until=?,attempts=attempts+1 WHERE sequence=?",
                (owner, now + lease_seconds, row["sequence"]),
            )
            return dict(db.execute(
                "SELECT * FROM records WHERE sequence=?", (row["sequence"],)
            ).fetchone())

    def release(self, sequence: int, *, delay: float, error: str) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE records SET claimed_by=NULL,claimed_until=NULL,next_attempt_at=?,last_error=? "
                "WHERE sequence=?",
                (time.time() + max(0.0, delay), sanitize_text(error, limit=300), sequence),
            )

    def acknowledge(self, sequence: int, ack: dict[str, Any], *, skipped: bool = False) -> None:
        state = "skipped" if skipped else "acked"
        try:
            with self.transaction() as db:
                self._validate_and_store_ack(db, sequence, ack)
                db.execute(
                    "UPDATE records SET state=?,claimed_by=NULL,claimed_until=NULL,last_error=NULL,"
                    "skipped_at=CASE WHEN ?='skipped' THEN ? ELSE skipped_at END WHERE sequence=?",
                    (state, state, time.time(), sequence),
                )
                self._cleanup(db)
        except jsonschema.ValidationError as exc:
            raise ProtocolError("V7 acknowledgement schema validation failed") from exc

    def resolve_remote_receipt(self, sequence: int, ack: dict[str, Any]) -> None:
        with self.transaction() as db:
            self._validate_and_store_ack(db, sequence, ack, allow_sequence_below_received=True)
            if sequence > int(self._meta(db, "highest_received_sequence", "0")):
                raise ProtocolError("receipt conflict sequence is not covered by remote stream")
            db.execute(
                "UPDATE records SET state='resolved_by_remote_receipt',claimed_by=NULL,"
                "claimed_until=NULL WHERE sequence=?",
                (sequence,),
            )
            self._cleanup(db)

    def mark_skip(self, sequence: int, reason: str, error: str) -> None:
        if reason not in SKIP_REASONS:
            raise ValueError(f"unsupported skip reason: {reason}")
        with self.transaction() as db:
            row = db.execute("SELECT * FROM records WHERE sequence=?", (sequence,)).fetchone()
            if row is None:
                return
            affected = [row]
            if row["object_type"] == "observation":
                affected = list(db.execute(
                    "SELECT * FROM records WHERE package_id=?", (row["package_id"],)
                ))
            elif row["object_type"] == "evidence":
                affected += list(db.execute(
                    "SELECT * FROM records WHERE parent_sequence=?", (sequence,)
                ))
            now = time.time()
            for item in affected:
                if item["state"] in TERMINAL_STATES:
                    continue
                db.execute(
                    "UPDATE records SET state='skip_pending',skip_reason=?,last_error=?,"
                    "claimed_by=NULL,claimed_until=NULL,skipped_at=? WHERE sequence=?",
                    (reason, sanitize_text(error, limit=300), now, item["sequence"]),
                )
                db.execute(
                    """INSERT OR REPLACE INTO dead_letters(sequence,object_type,object_id,
                       payload_sha256,reason,last_error,attempts,created_at,skipped_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (item["sequence"], item["object_type"], item["object_id"],
                     item["payload_sha256"], reason, sanitize_text(error, limit=300),
                     item["attempts"], item["outbox_created_at"], now),
                )
            self._trim_dead_letters(db)

    def evict_payload(self, sequence: int, reason: str) -> None:
        """Commit skip_pending before deleting evidence bytes."""
        self.mark_skip(sequence, reason, reason)
        with self._connect() as db:
            rows = list(db.execute(
                "SELECT evidence_path FROM records WHERE (sequence=? OR parent_sequence=?) "
                "AND evidence_path IS NOT NULL",
                (sequence, sequence),
            ))
        for row in rows:
            Path(row[0]).unlink(missing_ok=True)

    def enforce_limits(self) -> None:
        max_bytes = int(os.getenv("SENTINEL_OUTBOX_MAX_BYTES", str(2 * 1024 ** 3)))
        max_bytes = min(2 * 1024 ** 3, max(1024 ** 3, max_bytes))
        retention_days = max(1, int(os.getenv("SENTINEL_OUTBOX_RETENTION_DAYS", "7")))
        cutoff = time.time() - retention_days * 86400
        with self._connect() as db:
            expired = [int(row[0]) for row in db.execute(
                "SELECT sequence FROM records WHERE state NOT IN ('acked','skipped','resolved_by_remote_receipt') "
                "AND outbox_created_at<? ORDER BY sequence", (cutoff,)
            )]
        for sequence in expired:
            self.evict_payload(sequence, "retention_expired")
        while self._disk_usage() > max_bytes:
            target = int(max_bytes * 0.9)
            with self._connect() as db:
                row = db.execute(
                    "SELECT sequence FROM records WHERE object_type='evidence' AND state='pending' "
                    "ORDER BY outbox_created_at LIMIT 1"
                ).fetchone()
                if row is None:
                    row = db.execute(
                        "SELECT sequence FROM records WHERE object_type='observation' AND state='pending' "
                        "ORDER BY outbox_created_at LIMIT 1"
                    ).fetchone()
            if row is None:
                break
            self.evict_payload(
                int(row[0]), "evidence_expired" if self._record_kind(int(row[0])) == "evidence"
                else "outbox_overflow",
            )
            if self._disk_usage() <= target:
                break

    def bootstrap_state(self) -> dict[str, Any]:
        with self._connect() as db:
            return {
                "stream_id": self._meta(db, "stream_id", "") or None,
                "highest_contiguous_sequence": int(
                    self._meta(db, "highest_contiguous_sequence", "0")
                ),
                "highest_received_sequence": int(
                    self._meta(db, "highest_received_sequence", "0")
                ),
            }

    def metrics_summary(self, *, camera_id: str | None = None) -> dict[str, Any]:
        now = time.time()
        with self._connect() as db:
            artifacts = {}
            for kind in KINDS:
                camera_clause = " AND camera_id=?" if camera_id is not None else ""
                values: tuple[Any, ...] = (
                    (kind, camera_id) if camera_id is not None else (kind,)
                )
                row = db.execute(
                    """SELECT COUNT(*),COALESCE(SUM(LENGTH(payload_json)),0),MIN(outbox_created_at)
                       FROM records WHERE object_type=? AND state IN
                       ('pending','skip_pending','dead_letter')""" + camera_clause,
                    values,
                ).fetchone()
                artifacts[kind] = {
                    "pending_count": int(row[0]),
                    "pending_bytes": int(row[1]),
                    "oldest_pending_age_seconds": (
                        round(max(0.0, now - float(row[2])), 3) if row[2] is not None else 0.0
                    ),
                }
            return {
                "artifacts": artifacts,
                "outbox_records": int(db.execute("SELECT COUNT(*) FROM records").fetchone()[0]),
                "outbox_bytes": self._disk_usage(),
                "highest_contiguous_sequence": int(self._meta(db, "highest_contiguous_sequence", "0")),
                "highest_received_sequence": int(self._meta(db, "highest_received_sequence", "0")),
                "stream_id": self._meta(db, "stream_id", "") or None,
                "skip_pending": int(db.execute(
                    "SELECT COUNT(*) FROM records WHERE state='skip_pending'"
                ).fetchone()[0]),
            }

    def _validate_and_store_ack(self, db: sqlite3.Connection, sequence: int,
                                ack: dict[str, Any], *,
                                allow_sequence_below_received: bool = False) -> None:
        self.validators["acknowledgement"].validate(ack)
        expected_box = os.environ["SENTINEL_BOX_ID"]
        expected_generation = int(os.environ["SENTINEL_PLACEMENT_GENERATION"])
        if ack["deployment_id"] != self.deployment_id:
            raise ProtocolError("ack deployment_id mismatch")
        if ack["box_id"] != expected_box:
            raise ProtocolError("ack box_id mismatch")
        if int(ack["placement_generation"]) != expected_generation:
            raise ProtocolError("ack placement_generation mismatch")
        stream_id = self._meta(db, "stream_id", "")
        if stream_id and ack["stream_id"] != stream_id:
            raise ProtocolError("ack stream_id changed")
        contiguous = int(ack["highest_contiguous_sequence"])
        received = int(ack["highest_received_sequence"])
        if contiguous > received or int(ack["sequence"]) > received:
            raise ProtocolError("ack watermark invariant failed")
        old_contiguous = int(self._meta(db, "highest_contiguous_sequence", "0"))
        old_received = int(self._meta(db, "highest_received_sequence", "0"))
        if contiguous < old_contiguous or received < old_received:
            raise ProtocolError("ack watermark moved backwards")
        if not allow_sequence_below_received and int(ack["sequence"]) != int(sequence):
            raise ProtocolError("ack sequence mismatch")
        self._set_meta(db, "stream_id", str(ack["stream_id"]))
        self._set_meta(db, "highest_contiguous_sequence", str(contiguous))
        self._set_meta(db, "highest_received_sequence", str(received))

    def store_bootstrap(self, ack: dict[str, Any]) -> None:
        self.validators["acknowledgement"].validate(ack)
        with self.transaction() as db:
            local_stream = self._meta(db, "stream_id", "")
            if local_stream and local_stream != ack["stream_id"]:
                raise ProtocolError("bootstrap returned an unexpected stream_id")
            self._validate_and_store_ack(
                db, int(ack["sequence"]), ack, allow_sequence_below_received=True
            )
            next_sequence = int(self._meta(db, "next_sequence", "1"))
            if next_sequence == 1 and int(ack["highest_received_sequence"]) > 0:
                contiguous = int(ack["highest_contiguous_sequence"])
                received = int(ack["highest_received_sequence"])
                now = time.time()
                for sequence in range(contiguous + 1, received + 1):
                    object_id = str(uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"sentry-v7:{self.deployment_id}:lost:{sequence}",
                    ))
                    payload = request_json_bytes({
                        "sequence": sequence, "reason": "object_unreadable",
                    })
                    db.execute(
                        """INSERT OR IGNORE INTO records(sequence,package_id,camera_id,
                           object_type,object_id,parent_sequence,payload_json,payload_sha256,
                           state,skip_reason,outbox_created_at,last_error)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (sequence, f"lost-state-{sequence}", "__recovery__",
                         "observation", object_id, None, payload,
                         hashlib.sha256(payload).hexdigest(), "skip_pending",
                         "object_unreadable", now, "local V7 outbox state was lost"),
                    )
                self._set_meta(
                    db, "next_sequence", str(received + 1)
                )

    def _cleanup(self, db: sqlite3.Connection) -> None:
        watermark = int(self._meta(db, "highest_contiguous_sequence", "0"))
        packages = [row[0] for row in db.execute(
            "SELECT DISTINCT package_id FROM records WHERE sequence<=?", (watermark,)
        )]
        for package_id in packages:
            nonterminal = int(db.execute(
                "SELECT COUNT(*) FROM records WHERE package_id=? AND state NOT IN "
                "('acked','skipped','resolved_by_remote_receipt')", (package_id,)
            ).fetchone()[0])
            above = int(db.execute(
                "SELECT COUNT(*) FROM records WHERE package_id=? AND sequence>?",
                (package_id, watermark),
            ).fetchone()[0])
            if nonterminal or above:
                continue
            paths = [row[0] for row in db.execute(
                "SELECT evidence_path FROM records WHERE package_id=? AND evidence_path IS NOT NULL",
                (package_id,),
            )]
            db.execute("DELETE FROM records WHERE package_id=?", (package_id,))
            for value in paths:
                Path(value).unlink(missing_ok=True)

    def _trim_dead_letters(self, db: sqlite3.Connection) -> None:
        cutoff = time.time() - 7 * 86400
        db.execute("DELETE FROM dead_letters WHERE created_at<?", (cutoff,))
        db.execute(
            """DELETE FROM dead_letters WHERE sequence IN (
               SELECT sequence FROM dead_letters ORDER BY created_at DESC LIMIT -1 OFFSET 1000)"""
        )

    def _disk_usage(self) -> int:
        total = 0
        for path in (self.database, self.database.with_name(self.database.name + "-wal"),
                     self.database.with_name(self.database.name + "-shm")):
            try:
                total += path.stat().st_size
            except OSError:
                pass
        for path in self.evidence.glob("*"):
            try:
                total += path.stat().st_size
            except OSError:
                pass
        return total

    def _record_kind(self, sequence: int) -> str:
        with self._connect() as db:
            row = db.execute("SELECT object_type FROM records WHERE sequence=?", (sequence,)).fetchone()
        return str(row[0]) if row else "unknown"

    @staticmethod
    def _meta(db: sqlite3.Connection, key: str, default: str) -> str:
        row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row is not None else default

    @staticmethod
    def _set_meta(db: sqlite3.Connection, key: str, value: str) -> None:
        db.execute(
            "INSERT INTO metadata(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


class SentinelV7Uploader(threading.Thread):
    """One of potentially several camera-local workers draining one V7 stream."""

    def __init__(self, store: SentinelV7Outbox, base_url: str, token: str) -> None:
        super().__init__(
            name=f"sentinel-v7-uploader-{_safe_name(store.camera_id)}", daemon=True
        )
        self.store = store
        self.base_url = base_url.rstrip("/")
        parsed = urlsplit(self.base_url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("V7 ingest URL must be credential-free HTTPS")
        if not token or "\n" in token or "\r" in token:
            raise ValueError("Sentinel edge token is invalid")
        self.token = token
        self.mode = os.getenv("SENTINEL_DELIVERY_MODE", "").strip()
        if self.mode not in {"local_v7", "remote_v7"}:
            raise ValueError("SENTINEL_DELIVERY_MODE must be local_v7 or remote_v7")
        self.box_id = os.getenv("SENTINEL_BOX_ID", "").strip()
        try:
            uuid.UUID(self.box_id)
        except ValueError as exc:
            raise ValueError("SENTINEL_BOX_ID must be a UUID") from exc
        try:
            self.generation = int(os.environ["SENTINEL_PLACEMENT_GENERATION"])
        except (KeyError, ValueError) as exc:
            raise ValueError("SENTINEL_PLACEMENT_GENERATION must be a positive integer") from exc
        if self.generation < 1:
            raise ValueError("SENTINEL_PLACEMENT_GENERATION must be positive")
        self.box_credential = os.getenv("SENTINEL_BOX_CREDENTIAL", "").strip()
        if self.mode == "remote_v7" and not self.box_credential:
            raise ValueError("remote_v7 requires SENTINEL_BOX_CREDENTIAL")
        if self.mode == "local_v7" and self.box_credential:
            raise ValueError("local_v7 forbids SENTINEL_BOX_CREDENTIAL")
        ca_file = Path(os.getenv(
            "SENTINEL_INGEST_CA_FILE", "/run/secrets/sentinel/ingest/site-ca.crt"
        ))
        if not ca_file.is_file():
            raise ValueError(f"V7 site CA file is missing: {ca_file}")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = True
        context.load_verify_locations(cafile=str(ca_file))
        self.opener = build_opener(_NoRedirect(), HTTPSHandler(context=context))
        self.stop_event = threading.Event()
        self.owner = f"{os.getpid()}:{store.camera_id}:{uuid.uuid4()}"
        self.bootstrapped = False
        self.paused_protocol_error = False

    def run(self) -> None:
        delay = 0.25
        while not self.stop_event.is_set():
            record: dict[str, Any] | None = None
            if self.paused_protocol_error:
                self.stop_event.wait(5)
                continue
            try:
                if not self.bootstrapped:
                    self._bootstrap_stream()
                    self.bootstrapped = True
                record = self.store.claim(self.owner, limit=8)
                if record is None:
                    self.store.enforce_limits()
                    self.store.metrics.snapshot(self.store)
                    self.stop_event.wait(0.25)
                    continue
                self._submit(record)
                delay = 0.25
            except RetryLater as exc:
                self.store.metrics.retry(exc.kind, exc.reason, exc.status)
                delay = min(60.0, max(0.25, delay * 2.0)) * random.uniform(0.75, 1.25)
                if record:
                    self.store.release(int(record["sequence"]), delay=delay, error=exc.reason)
                self.stop_event.wait(delay)
            except ProtocolError:
                self.paused_protocol_error = True
                if record:
                    self.store.release(
                        int(record["sequence"]), delay=60, error="protocol_error"
                    )
            except Exception as exc:
                if record:
                    self.store.release(
                        int(record["sequence"]), delay=5,
                        error=f"unexpected:{type(exc).__name__}",
                    )
                self.stop_event.wait(1)

    def stop(self) -> None:
        self.stop_event.set()
        if self.is_alive():
            self.join(timeout=5)

    def _bootstrap_stream(self) -> None:
        ack = self._request_json("GET", "/api/v7/ingest/stream", None, "stream", None)
        try:
            self.store.store_bootstrap(ack)
        except (jsonschema.ValidationError, ProtocolError) as exc:
            raise ProtocolError("invalid V7 stream bootstrap response") from exc

    def _submit(self, record: dict[str, Any]) -> None:
        sequence = int(record["sequence"])
        kind = str(record["object_type"])
        if record["state"] == "skip_pending":
            try:
                ack = self._request_json(
                    "POST", "/api/v7/ingest/skip",
                    {"sequence": sequence, "reason": record["skip_reason"]},
                    "skip", None,
                )
                self.store.acknowledge(sequence, ack, skipped=True)
                self.store.metrics.skips_total += 1
            except HTTPError as exc:
                if exc.code != 409:
                    raise
                state = self._request_json(
                    "GET", "/api/v7/ingest/stream", None, "stream", None
                )
                self.store.resolve_remote_receipt(sequence, state)
            return
        payload = bytes(record["payload_json"])
        endpoint = {
            "observation": "/api/v7/ingest/events",
            "embedding": "/api/v7/ingest/embeddings",
            "evidence": "/api/v7/ingest/evidence",
        }[kind]
        try:
            if kind == "evidence":
                path = Path(str(record["evidence_path"]))
                try:
                    image = path.read_bytes()
                except OSError:
                    self.store.mark_skip(sequence, "object_unreadable", "evidence file missing")
                    return
                metadata = json.loads(payload)
                if "sha256:" + hashlib.sha256(image).hexdigest() != record["content_sha256"]:
                    self.store.mark_skip(sequence, "object_unreadable", "evidence hash changed")
                    return
                body, content_type = self._multipart(metadata, image)
                ack = self._request_bytes("POST", endpoint, body, content_type, kind, sequence)
            else:
                ack = self._request_bytes(
                    "POST", endpoint, payload, "application/json", kind, sequence
                )
            self.store.acknowledge(sequence, ack)
            self.store.metrics.acknowledged(kind)
        except HTTPError as exc:
            if exc.code == 409:
                state = self._request_json(
                    "GET", "/api/v7/ingest/stream", None, "stream", None
                )
                self.store.resolve_remote_receipt(sequence, state)
                return
            if exc.code in {400, 422}:
                self.store.mark_skip(sequence, "validation_failed", f"HTTP {exc.code}")
                return
            raise

    def _request_json(self, method: str, endpoint: str, payload: dict | None,
                      kind: str, sequence: int | None) -> dict[str, Any]:
        body = None if payload is None else request_json_bytes(payload)
        return self._request_bytes(
            method, endpoint, body, "application/json", kind, sequence
        )

    def _request_bytes(self, method: str, endpoint: str, body: bytes | None,
                       content_type: str, kind: str,
                       sequence: int | None) -> dict[str, Any]:
        if kind in KINDS:
            self.store.metrics.attempt(kind)
        headers: dict[str, str] = {
            "Authorization": "Bearer " + self.token,
            "Accept": "application/json",
            "X-Sentry-Delivery-Mode": self.mode,
            "X-Sentry-Placement-Generation": str(self.generation),
        }
        if body is not None:
            headers["Content-Type"] = content_type
        if sequence is not None:
            headers["X-Sentry-Stream-Sequence"] = str(sequence)
        if self.mode == "remote_v7":
            headers["X-Sentry-Box-Credential"] = self.box_credential
        request = Request(
            self.base_url + endpoint, data=body, method=method, headers=headers
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                if response.status != 200:
                    raise RetryLater(
                        f"unexpected HTTP {response.status}", kind=kind,
                        status=response.status, reason="http_unexpected",
                    )
                value = json.load(response)
                if not isinstance(value, dict):
                    raise ValueError("acknowledgement is not an object")
                return value
        except HTTPError as exc:
            reason = sanitize_text(getattr(exc, "reason", None) or "http_response", limit=80)
            if kind in KINDS:
                self.store.metrics.rejected(kind, exc.code, reason)
            if exc.code in {400, 409, 422}:
                raise
            if exc.code in {401, 403, 408, 425, 429} or exc.code >= 500:
                raise RetryLater(
                    f"retryable HTTP {exc.code}", kind=kind, status=exc.code,
                    reason="authorization_paused" if exc.code in {401, 403}
                    else "http_retryable",
                ) from exc
            raise RetryLater(
                f"unexpected HTTP {exc.code}", kind=kind, status=exc.code,
                reason="http_unexpected",
            ) from exc
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise RetryLater(
                "management transport unavailable or malformed acknowledgement",
                kind=kind, reason="network_or_ack_error",
            ) from exc

    @staticmethod
    def _multipart(metadata: dict[str, Any], image: bytes) -> tuple[bytes, str]:
        boundary = "sentinel-v7-" + hashlib.sha256(
            str(metadata["evidence_id"]).encode()
        ).hexdigest()
        metadata_body = canonical_json_bytes(metadata)
        body = b"".join([
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"metadata\"\r\n"
            "Content-Type: application/json\r\n\r\n".encode(),
            metadata_body,
            f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"{metadata['evidence_id']}\"\r\nContent-Type: "
            f"{metadata['content_type']}\r\n\r\n".encode(),
            image,
            f"\r\n--{boundary}--\r\n".encode(),
        ])
        return body, f"multipart/form-data; boundary={boundary}"
