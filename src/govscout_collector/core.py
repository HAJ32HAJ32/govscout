from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import ssl
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from govscout.fca_discovery import FCA_MAX_RESPONSE_BYTES, parse_fca_json

UPLOAD_ENDPOINT = "https://leads.misegroup.co.uk/api/v1/collector/imports"
RESPONSE_LIMIT_BYTES = 16_384
OUTBOX_PENDING_BATCH_LIMIT = 25
OUTBOX_PENDING_BYTES_LIMIT = 25_000_000
OUTBOX_RETAINED_BATCH_LIMIT = 100
OUTBOX_RETENTION = timedelta(days=30)


class UploadUnavailable(RuntimeError):
    """The upload outcome is safely retryable."""


class UploadRejected(RuntimeError):
    """GovScout definitively refused the batch."""


class CollectorQueueFull(RuntimeError):
    """The durable outbox has reached its bounded local storage limit."""


@dataclass(frozen=True, slots=True)
class PendingBatch:
    import_id: str
    payload_sha256: str
    payload: bytes


@dataclass(frozen=True, slots=True)
class UploadReceipt:
    import_id: str
    payload_sha256: str
    state: str


@dataclass(frozen=True, slots=True)
class SyncResult:
    uploaded: int
    pending: int
    errors: tuple[str, ...]


class UploadTransport(Protocol):
    def upload(self, *, import_id: str, payload: bytes, token: str) -> UploadReceipt: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise UploadRejected("GovScout refused a redirected upload")


def is_valid_upload_token(token: str) -> bool:
    try:
        prefix, device_id, secret = token.split("_", 2)
        token.encode("ascii")
    except (AttributeError, ValueError, UnicodeEncodeError):
        return False
    return (
        prefix == "gsc"
        and len(device_id) == 32
        and all(character in "0123456789abcdef" for character in device_id)
        and len(secret) == 43
        and all(
            character
            in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
            for character in secret
        )
    )


class GovScoutUploadTransport:
    def __init__(self, *, opener=None, timeout: float = 30.0) -> None:
        self._opener = opener or build_opener(
            HTTPSHandler(context=ssl.create_default_context()),
            _NoRedirect(),
        )
        self._timeout = timeout

    def upload(self, *, import_id: str, payload: bytes, token: str) -> UploadReceipt:
        if not is_valid_upload_token(token):
            raise UploadRejected("Collector setup credential is invalid")
        if len(payload) > FCA_MAX_RESPONSE_BYTES:
            raise UploadRejected("Collector batch exceeds the upload limit")
        payload_sha256 = hashlib.sha256(payload).hexdigest()
        request = Request(
            UPLOAD_ENDPOINT,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Idempotency-Key": import_id,
                "X-Payload-SHA256": payload_sha256,
                "User-Agent": "GovScout-Collector/0.1",
            },
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                status = response.status
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0]
                response_body = response.read(RESPONSE_LIMIT_BYTES + 1)
        except HTTPError as exc:
            if exc.code in {401, 403, 409, 413, 415, 422}:
                raise UploadRejected(f"GovScout refused the upload (HTTP {exc.code})") from None
            raise UploadUnavailable("GovScout is temporarily unavailable") from None
        except (OSError, TimeoutError, URLError):
            raise UploadUnavailable("GovScout is temporarily unavailable") from None
        if status not in {200, 202}:
            raise UploadUnavailable("GovScout returned an unexpected response")
        if content_type != "application/json" or len(response_body) > RESPONSE_LIMIT_BYTES:
            raise UploadUnavailable("GovScout returned an invalid receipt")
        try:
            decoded = json.loads(response_body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise UploadUnavailable("GovScout returned an invalid receipt") from None
        if type(decoded) is not dict or set(decoded) != {
            "import_id",
            "payload_sha256",
            "state",
        }:
            raise UploadUnavailable("GovScout returned an invalid receipt")
        if (
            decoded["import_id"] != import_id
            or decoded["payload_sha256"] != payload_sha256
            or decoded["state"] not in {"pending", "accepted", "rejected"}
        ):
            raise UploadUnavailable("GovScout returned a mismatched receipt")
        return UploadReceipt(
            import_id=decoded["import_id"],
            payload_sha256=decoded["payload_sha256"],
            state=decoded["state"],
        )


class CollectorQueue:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            os.chmod(self.path.parent, 0o700)
        conn = self.connection()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS batches (
                    import_id TEXT PRIMARY KEY,
                    payload_sha256 TEXT NOT NULL,
                    payload BLOB NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('pending', 'uploaded')),
                    staged_at TEXT NOT NULL,
                    uploaded_at TEXT,
                    receipt_state TEXT CHECK (
                        receipt_state IS NULL OR receipt_state IN ('pending', 'accepted', 'rejected')
                    ),
                    CHECK (
                        (state = 'pending' AND uploaded_at IS NULL AND receipt_state IS NULL)
                        OR (state = 'uploaded' AND uploaded_at IS NOT NULL AND receipt_state IS NOT NULL)
                    )
                );
                DROP TRIGGER IF EXISTS batches_no_delete;
                CREATE TRIGGER IF NOT EXISTS batches_pending_no_delete
                BEFORE DELETE ON batches WHEN OLD.state = 'pending' BEGIN
                    SELECT RAISE(ABORT, 'pending collector batches cannot be deleted');
                END;
                CREATE TRIGGER IF NOT EXISTS batches_payload_immutable
                BEFORE UPDATE OF import_id, payload_sha256, payload, staged_at ON batches BEGIN
                    SELECT RAISE(ABORT, 'collector batch payload is immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS batches_terminal_immutable
                BEFORE UPDATE ON batches WHEN OLD.state = 'uploaded' BEGIN
                    SELECT RAISE(ABORT, 'uploaded collector batches are immutable');
                END;
                """
            )
        finally:
            conn.close()

    def connection(self) -> sqlite3.Connection:
        if not self.path.exists():
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.close(descriptor)
        if os.name != "nt":
            os.chmod(self.path, 0o600)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def stage(self, payload: bytes, *, now: datetime | None = None) -> PendingBatch:
        if not isinstance(payload, bytes):
            raise TypeError("collector payload must be bytes")
        parse_fca_json(payload)
        payload_sha256 = hashlib.sha256(payload).hexdigest()
        instant = (now or datetime.now(UTC)).astimezone(UTC)
        conn = self.connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            retention_cutoff = (instant - OUTBOX_RETENTION).isoformat()
            conn.execute(
                "DELETE FROM batches WHERE state = 'uploaded' AND uploaded_at < ?",
                (retention_cutoff,),
            )
            existing = conn.execute(
                """
                SELECT import_id FROM batches
                WHERE payload_sha256 = ? AND state = 'pending'
                ORDER BY staged_at, import_id LIMIT 1
                """,
                (payload_sha256,),
            ).fetchone()
            if existing is not None:
                import_id = existing["import_id"]
            else:
                usage = conn.execute(
                    """
                    SELECT
                        count(*) AS retained_count,
                        count(*) FILTER (WHERE state = 'pending') AS pending_count,
                        coalesce(sum(length(payload)) FILTER (WHERE state = 'pending'), 0)
                            AS pending_bytes
                    FROM batches
                    """
                ).fetchone()
                if (
                    usage["retained_count"] >= OUTBOX_RETAINED_BATCH_LIMIT
                    or usage["pending_count"] >= OUTBOX_PENDING_BATCH_LIMIT
                    or usage["pending_bytes"] + len(payload) > OUTBOX_PENDING_BYTES_LIMIT
                ):
                    raise CollectorQueueFull(
                        "Collector outbox is full; retry pending uploads before collecting again"
                    )
                import_id = uuid.uuid4().hex
                conn.execute(
                    """
                    INSERT INTO batches (
                        import_id, payload_sha256, payload, state, staged_at
                    ) VALUES (?, ?, ?, 'pending', ?)
                    """,
                    (import_id, payload_sha256, payload, instant.isoformat()),
                )
            conn.commit()
        finally:
            conn.close()
        return PendingBatch(import_id, payload_sha256, payload)

    def pending(self) -> tuple[PendingBatch, ...]:
        conn = self.connection()
        try:
            rows = conn.execute(
                """
                SELECT import_id, payload_sha256, payload
                FROM batches WHERE state = 'pending'
                ORDER BY staged_at, import_id
                """
            ).fetchall()
            return tuple(
                PendingBatch(row["import_id"], row["payload_sha256"], bytes(row["payload"]))
                for row in rows
            )
        finally:
            conn.close()

    def retry_pending(self, *, transport: UploadTransport, token: str) -> SyncResult:
        uploaded = 0
        errors: list[str] = []
        for batch in self.pending():
            try:
                receipt = transport.upload(
                    import_id=batch.import_id,
                    payload=batch.payload,
                    token=token,
                )
            except (UploadRejected, UploadUnavailable) as exc:
                errors.append(str(exc))
                continue
            if (
                receipt.import_id != batch.import_id
                or receipt.payload_sha256 != batch.payload_sha256
            ):
                errors.append("GovScout returned a mismatched receipt")
                continue
            if receipt.state == "pending":
                errors.append("GovScout is still processing the batch; retry later")
                continue
            conn = self.connection()
            try:
                updated = conn.execute(
                    """
                    UPDATE batches
                    SET state = 'uploaded', uploaded_at = ?, receipt_state = ?
                    WHERE import_id = ? AND state = 'pending'
                    """,
                    (datetime.now(UTC).isoformat(), receipt.state, batch.import_id),
                )
                conn.commit()
                if receipt.state == "accepted":
                    uploaded += updated.rowcount
                elif updated.rowcount:
                    errors.append("GovScout rejected the batch; no firms were imported")
            finally:
                conn.close()
        return SyncResult(uploaded=uploaded, pending=len(self.pending()), errors=tuple(errors))
