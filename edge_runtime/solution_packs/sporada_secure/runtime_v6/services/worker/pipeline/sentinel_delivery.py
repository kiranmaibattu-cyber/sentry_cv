"""Durable Sentinel V6.0 event, evidence, and embedding delivery."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import random
import re
import ssl
import threading
import time
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, Request, build_opener

import jsonschema

from .structured_logging import sanitize_text


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


class PermanentRejection(RuntimeError):
    def __init__(self, stage: str, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.stage = stage
        self.status = status


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, separators=(",", ":"), allow_nan=False),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(value))[:160] or "camera"


class SentinelDeliveryMetrics:
    KINDS = ("observation", "evidence", "embedding")

    def __init__(self, state_root: Path, camera_id: str) -> None:
        self.camera_id = str(camera_id)
        self.path = state_root / "metrics" / f"sentinel-delivery-{_safe_name(camera_id)}.json"
        self.lock = threading.Lock()
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            loaded = {}
        self.data = loaded if isinstance(loaded, dict) else {}
        self.data.update(schema_version="1.0", camera_id=self.camera_id)
        artifacts = self.data.setdefault("artifacts", {})
        for kind in self.KINDS:
            item = artifacts.setdefault(kind, {})
            item.setdefault("upload_attempts_total", 0)
            item.setdefault("acknowledgements_total", 0)
            item.setdefault("retries_total", 0)
            item.setdefault("http_rejections", {})
            item.setdefault("retry_reasons", {})
        self.data.setdefault("dead_letters_total", 0)
        self.data.setdefault("dead_letter_reasons", {})
        self._last_pending_scan = 0.0

    def attempt(self, kind: str) -> None:
        self._increment(kind, "upload_attempts_total")

    def acknowledged(self, kind: str) -> None:
        self._increment(kind, "acknowledgements_total")

    def retry(self, kind: str, reason: str, status: int | None = None) -> None:
        with self.lock:
            item = self._artifact(kind)
            item["retries_total"] = int(item.get("retries_total", 0)) + 1
            key = str(status) if status is not None else str(reason)[:120]
            reasons = item.setdefault("retry_reasons", {})
            reasons[key] = int(reasons.get(key, 0)) + 1
            self._write_locked()

    def rejected(self, kind: str, status: int, reason: str) -> None:
        with self.lock:
            item = self._artifact(kind)
            key = f"{status}:{str(reason)[:80]}"
            values = item.setdefault("http_rejections", {})
            values[key] = int(values.get(key, 0)) + 1
            self._write_locked()

    def dead_letter(self, reason: str) -> None:
        with self.lock:
            self.data["dead_letters_total"] = int(
                self.data.get("dead_letters_total", 0)
            ) + 1
            values = self.data.setdefault("dead_letter_reasons", {})
            key = str(reason)[:120]
            values[key] = int(values.get(key, 0)) + 1
            self._write_locked()

    def pending(self, outbox: Path, *, force: bool = False) -> None:
        # A backlog may contain many records. Scanning it for every newly
        # persisted observation turns event production into O(n^2) work.
        # Pending gauges may lag by at most one second; terminal delivery
        # transitions force an immediate refresh.
        monotonic_now = time.monotonic()
        with self.lock:
            if not force and monotonic_now - self._last_pending_scan < 1.0:
                return
            self._last_pending_scan = monotonic_now
        now = time.time()
        summary = {
            kind: {"pending_count": 0, "pending_bytes": 0, "oldest_created_at": None}
            for kind in self.KINDS
        }
        record_count = 0
        record_bytes = 0
        for path in outbox.glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                record_bytes += path.stat().st_size
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            record_count += 1
            created = float(record.get("created_at") or path.stat().st_mtime)
            delivery = record.get("delivery") or {}
            if not delivery.get("observation_ack"):
                self._pending_item(
                    summary["observation"], created,
                    len(json.dumps(record.get("observation") or {}, separators=(",", ":")).encode()),
                )
            evidence_acks = delivery.get("evidence_acks") or {}
            for asset in record.get("evidence") or []:
                metadata = asset.get("metadata") or {}
                if metadata.get("evidence_id") in evidence_acks:
                    continue
                size = len(json.dumps(metadata, separators=(",", ":")).encode())
                try:
                    size += Path(asset["path"]).stat().st_size
                except (OSError, KeyError, TypeError):
                    pass
                self._pending_item(summary["evidence"], created, size)
            embedding_acks = delivery.get("embedding_acks") or {}
            for embedding in record.get("embeddings") or []:
                if embedding.get("embedding_id") in embedding_acks:
                    continue
                self._pending_item(
                    summary["embedding"], created,
                    len(json.dumps(embedding, separators=(",", ":")).encode()),
                )
        with self.lock:
            for kind, pending in summary.items():
                oldest = pending.pop("oldest_created_at")
                pending["oldest_pending_age_seconds"] = (
                    round(max(0.0, now - float(oldest)), 3) if oldest is not None else 0.0
                )
                self._artifact(kind).update(pending)
            self.data["outbox_records"] = record_count
            self.data["outbox_record_bytes"] = record_bytes
            self._write_locked()

    @staticmethod
    def _pending_item(target: dict[str, Any], created: float, size: int) -> None:
        target["pending_count"] += 1
        target["pending_bytes"] += max(0, int(size))
        oldest = target["oldest_created_at"]
        target["oldest_created_at"] = created if oldest is None else min(oldest, created)

    def _increment(self, kind: str, field: str) -> None:
        with self.lock:
            item = self._artifact(kind)
            item[field] = int(item.get(field, 0)) + 1
            self._write_locked()

    def _artifact(self, kind: str) -> dict[str, Any]:
        normalized = kind if kind in self.KINDS else "observation"
        return self.data.setdefault("artifacts", {}).setdefault(normalized, {})

    def _write_locked(self) -> None:
        self.data["observed_at"] = time.time()
        atomic_json(self.path, self.data)


class SentinelV6Outbox:
    def __init__(self, state_root: Path, camera_id: str) -> None:
        self.root = state_root / "sentinel_v6"
        self.outbox = self.root / "outbox" / camera_id
        self.evidence = self.root / "evidence" / camera_id
        self.dead_letter = self.root / "dead-letter" / camera_id
        self.outbox.mkdir(parents=True, exist_ok=True)
        self.evidence.mkdir(parents=True, exist_ok=True)
        self.dead_letter.mkdir(parents=True, exist_ok=True)
        self.metrics = SentinelDeliveryMetrics(state_root, camera_id)
        schema_dir = Path(__file__).resolve().parents[3] / "image_schema"
        self.validators = {
            kind: jsonschema.Draft202012Validator(
                json.loads((schema_dir / f"{kind}.schema.json").read_text(encoding="utf-8")),
                format_checker=jsonschema.FormatChecker(),
            ) for kind in ("observation", "evidence", "embedding", "acknowledgement")
        }

    def persist(
        self,
        observation: dict[str, Any],
        evidence: list[tuple[dict[str, Any], bytes]],
        embeddings: list[dict[str, Any]] | None = None,
        scenes: list[dict[str, Any]] | None = None,
    ) -> Path:
        observation_id = str(observation["observation_id"])
        self.validators["observation"].validate(observation)
        for metadata, _ in evidence:
            self.validators["evidence"].validate(metadata)
            if metadata["observation_id"] != observation_id:
                raise ValueError("evidence parent does not match observation")
        evidence_ids = {metadata["evidence_id"] for metadata, _ in evidence}
        embedding_ids = {item["embedding_id"] for item in embeddings or []}
        if set(observation.get("evidence_ids") or []) != evidence_ids:
            raise ValueError("observation evidence_ids do not match stored assets")
        if set(observation.get("embedding_ids") or []) != embedding_ids:
            raise ValueError("observation embedding_ids do not match stored embeddings")
        for embedding in embeddings or []:
            self.validators["embedding"].validate(embedding)
            if embedding["observation_id"] != observation_id or embedding["evidence_id"] not in evidence_ids:
                raise ValueError("embedding parent/evidence does not match observation")
        assets = []
        written: list[Path] = []
        try:
            for metadata, body in evidence:
                evidence_id = str(metadata["evidence_id"])
                suffix = {
                    "image/png": ".png", "image/jpeg": ".jpg", "video/mp4": ".mp4",
                }[metadata["content_type"]]
                path = self.evidence / f"{evidence_id}{suffix}"
                digest = "sha256:" + hashlib.sha256(body).hexdigest()
                if metadata.get("sha256") != digest or metadata.get("size_bytes") != len(body):
                    raise ValueError("evidence metadata does not match exact bytes")
                temporary = path.with_suffix(path.suffix + ".tmp")
                temporary.write_bytes(body)
                os.replace(temporary, path)
                written.append(path)
                assets.append({"metadata": metadata, "path": str(path)})

            record = {
                "outbox_schema_version": 1,
                "created_at": time.time(),
                "observation": observation,
                "evidence": assets,
                "embeddings": list(embeddings or []),
                "scenes": [],
                "delivery": {
                    "observation_ack": None,
                    "evidence_acks": {},
                    "embedding_acks": {},
                    "attempts": 0,
                },
            }
            path = self.outbox / f"{observation_id}.json"
            atomic_json(path, record)
            self.metrics.pending(self.outbox)
            return path
        except Exception:
            for path in written:
                path.unlink(missing_ok=True)
            raise


class SentinelV6Uploader(threading.Thread):
    RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504}
    PERMANENT_STATUSES = {400, 404, 405, 409, 413, 415, 422}

    def __init__(self, store: SentinelV6Outbox, base_url: str, token: str) -> None:
        super().__init__(name=f"sentinel-v6-uploader-{store.outbox.name}", daemon=True)
        self.store = store
        self.base_url = base_url.rstrip("/")
        parsed = urlsplit(self.base_url)
        private_http = (
            parsed.scheme == "http"
            and parsed.hostname is not None
            and (
                parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                or parsed.hostname.endswith(".svc")
                or parsed.hostname.endswith(".svc.cluster.local")
            )
        )
        if (
            (parsed.scheme != "https" and not private_http)
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Sentinel ingest URL must be HTTPS or private cluster HTTP")
        if not token or "\n" in token or "\r" in token:
            raise ValueError("Sentinel edge token is invalid")
        self.token = token
        self.stop_event = threading.Event()
        self.opener = build_opener(
            _NoRedirect(), HTTPSHandler(context=ssl.create_default_context())
        )

    def run(self) -> None:
        delay = 0.25
        while not self.stop_event.is_set():
            progressed = False
            for path in sorted(self.store.outbox.glob("*.json")):
                if self.stop_event.is_set():
                    return
                try:
                    self.submit(path)
                    progressed = True
                    delay = 0.25
                except RetryLater as exc:
                    self.store.metrics.retry(exc.kind, exc.reason, exc.status)
                    delay = min(60.0, delay * 2.0) * random.uniform(0.75, 1.25)
                    break
                except PermanentRejection as exc:
                    self._dead_letter(path, exc)
                    progressed = True
                except Exception as exc:
                    self._dead_letter(
                        path,
                        PermanentRejection("record", f"invalid outbox record: {type(exc).__name__}"),
                    )
                    progressed = True
            self.store.metrics.pending(self.store.outbox)
            self.stop_event.wait(0.25 if progressed else delay)

    def stop(self) -> None:
        self.stop_event.set()
        if self.is_alive():
            self.join(timeout=5)

    def submit(self, path: Path) -> None:
        record = json.loads(path.read_text(encoding="utf-8"))
        delivery = record["delivery"]
        delivery["attempts"] = int(delivery.get("attempts", 0)) + 1
        atomic_json(path, record)

        if not delivery.get("observation_ack"):
            delivery["observation_ack"] = self._post_json(
                "/api/v6/ingest/events", record["observation"],
                "observation", record["observation"]["observation_id"],
            )
            atomic_json(path, record)

        for asset in record["evidence"]:
            metadata = asset["metadata"]
            evidence_id = metadata["evidence_id"]
            if evidence_id in delivery["evidence_acks"]:
                continue
            evidence_path = Path(asset["path"])
            try:
                body = evidence_path.read_bytes()
            except OSError as exc:
                raise PermanentRejection("evidence", "persisted evidence is missing") from exc
            digest = "sha256:" + hashlib.sha256(body).hexdigest()
            if digest != metadata["sha256"] or len(body) != metadata["size_bytes"]:
                raise PermanentRejection("evidence", "persisted evidence bytes changed")
            response = self._post_multipart(metadata, body)
            self._validate_ack(response, "evidence", evidence_id)
            if response.get("sha256") != digest:
                raise PermanentRejection("evidence", "evidence acknowledgement checksum mismatch")
            delivery["evidence_acks"][evidence_id] = response
            self.store.metrics.acknowledged("evidence")
            atomic_json(path, record)

        for embedding in record["embeddings"]:
            embedding_id = embedding["embedding_id"]
            if embedding_id in delivery["embedding_acks"]:
                continue
            delivery["embedding_acks"][embedding_id] = self._post_json(
                "/api/v6/ingest/embeddings", embedding, "embedding", embedding_id
            )
            atomic_json(path, record)

        for asset in record["evidence"]:
            Path(asset["path"]).unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        self.store.metrics.pending(self.store.outbox, force=True)

    def _post_json(
        self, endpoint: str, payload: dict[str, Any], kind: str, expected_id: str
    ) -> dict[str, Any]:
        body = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")
        response = self._request(endpoint, body, "application/json", kind)
        self._validate_ack(response, kind, expected_id)
        self.store.metrics.acknowledged(kind)
        return response

    def _post_multipart(self, metadata: dict[str, Any], body: bytes) -> dict[str, Any]:
        boundary = "sentinel-" + hashlib.sha256(metadata["evidence_id"].encode()).hexdigest()
        chunks = [
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"metadata\"\r\n"
            "Content-Type: application/json\r\n\r\n".encode(),
            json.dumps(metadata, separators=(",", ":"), allow_nan=False).encode(),
            f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"{metadata['evidence_id']}\"\r\nContent-Type: "
            f"{metadata['content_type']}\r\n\r\n".encode(),
            body,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
        return self._request(
            "/api/v6/ingest/evidence",
            b"".join(chunks),
            f"multipart/form-data; boundary={boundary}",
            "evidence",
        )

    def _request(self, endpoint: str, body: bytes, content_type: str,
                 kind: str) -> dict[str, Any]:
        self.store.metrics.attempt(kind)
        request = Request(
            self.base_url + endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": content_type,
                "Accept": "application/json",
            },
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                if response.status not in (200, 201):
                    raise PermanentRejection("transport", f"unexpected HTTP {response.status}")
                try:
                    payload = json.load(response)
                except (ValueError, json.JSONDecodeError) as exc:
                    raise RetryLater(
                        "malformed JSON acknowledgement", kind=kind,
                        reason="malformed_acknowledgement",
                    ) from exc
                if not isinstance(payload, dict):
                    raise RetryLater(
                        "malformed acknowledgement object", kind=kind,
                        reason="malformed_acknowledgement",
                    )
                return payload
        except HTTPError as exc:
            rejection_reason = sanitize_text(
                getattr(exc, "reason", None) or "http_response", limit=80
            )
            self.store.metrics.rejected(kind, exc.code, rejection_reason)
            if exc.code in self.RETRY_STATUSES or exc.code in {401, 403}:
                raise RetryLater(
                    f"retryable HTTP {exc.code}", kind=kind, status=exc.code,
                    reason="http_retryable",
                ) from exc
            if exc.code in self.PERMANENT_STATUSES:
                raise PermanentRejection("management", f"HTTP {exc.code}", exc.code) from exc
            if 400 <= exc.code < 500:
                raise PermanentRejection("management", f"HTTP {exc.code}", exc.code) from exc
            raise RetryLater(
                f"unexpected HTTP {exc.code}", kind=kind, status=exc.code,
                reason="http_unexpected",
            ) from exc
        except OSError as exc:
            raise RetryLater(
                "management transport unavailable", kind=kind,
                reason="network_error",
            ) from exc

    def _validate_ack(self, response: dict[str, Any], kind: str, expected_id: str) -> None:
        try:
            self.store.validators["acknowledgement"].validate(response)
        except jsonschema.ValidationError as exc:
            raise RetryLater(
                "malformed V6 acknowledgement", kind=kind,
                reason="malformed_acknowledgement",
            ) from exc
        if response.get("kind") != kind:
            raise RetryLater(
                "acknowledgement kind does not match submitted artifact", kind=kind,
                reason="acknowledgement_kind_mismatch",
            )
        if response.get("id") != expected_id:
            raise RetryLater(
                "acknowledgement id does not match submitted artifact", kind=kind,
                reason="acknowledgement_id_mismatch",
            )

    def _dead_letter(self, path: Path, error: PermanentRejection) -> None:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            record = {"delivery": {}}
        delivery = record.setdefault("delivery", {})
        delivery["permanent_error"] = {
            "stage": error.stage,
            "status": error.status,
            "message": str(error),
            "at": time.time(),
        }
        target = self.store.dead_letter / path.name
        atomic_json(path, record)
        os.replace(path, target)
        self.store.metrics.dead_letter(f"{error.stage}:{error.status or 'none'}")
        self.store.metrics.pending(self.store.outbox, force=True)
