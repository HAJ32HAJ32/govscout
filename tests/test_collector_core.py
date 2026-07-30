import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

import govscout_collector.core as collector_core
from govscout_collector.core import (
    CollectorQueueFull,
    CollectorQueue,
    GovScoutUploadTransport,
    UploadReceipt,
    UploadUnavailable,
)


def _payload(frn="123456"):
    return json.dumps(
        {
            "firms": [
                {
                    "frn": frn,
                    "firm_name": "Example Finance Ltd",
                    "status": "Authorised",
                    "firm_type": "Regulated firm",
                    "source_url": f"https://register.fca.org.uk/s/firm?id={frn}",
                    "website_url": "https://example.test/",
                    "location": "London",
                    "company_number": "12345678",
                }
            ]
        },
        separators=(",", ":"),
    ).encode()


def test_collector_queue_preserves_failed_upload_and_retries_idempotently(tmp_path):
    queue = CollectorQueue(tmp_path / "collector.sqlite3")
    batch = queue.stage(_payload())

    class FlakyTransport:
        attempts = 0

        def upload(self, *, import_id, payload, token):
            self.attempts += 1
            assert import_id == batch.import_id
            assert payload == _payload()
            assert token == "device-token"
            if self.attempts == 1:
                raise UploadUnavailable("GovScout is temporarily unavailable")
            return UploadReceipt(
                import_id=import_id,
                payload_sha256=batch.payload_sha256,
                state="pending",
            )

    transport = FlakyTransport()
    first = queue.retry_pending(transport=transport, token="device-token")
    second = queue.retry_pending(transport=transport, token="device-token")

    assert first.uploaded == 0
    assert first.pending == 1
    assert first.errors == ("GovScout is temporarily unavailable",)
    assert second.uploaded == 0
    assert second.pending == 1
    assert second.errors == ("GovScout is still processing the batch; retry later",)
    assert queue.pending() == (batch,)
    stored = queue.connection().execute(
        "SELECT state, receipt_state FROM batches WHERE import_id = ?", (batch.import_id,)
    ).fetchone()
    assert tuple(stored) == ("pending", None)


def test_collector_queue_reports_rejected_import_without_claiming_success(tmp_path):
    queue = CollectorQueue(tmp_path / "collector.sqlite3")
    batch = queue.stage(_payload())

    class RejectingTransport:
        def upload(self, *, import_id, payload, token):
            return UploadReceipt(
                import_id=import_id,
                payload_sha256=hashlib.sha256(payload).hexdigest(),
                state="rejected",
            )

    result = queue.retry_pending(transport=RejectingTransport(), token="device-token")

    assert result.uploaded == 0
    assert result.pending == 0
    assert result.errors == ("GovScout rejected the batch; no firms were imported",)
    stored = queue.connection().execute(
        "SELECT state, receipt_state FROM batches WHERE import_id = ?", (batch.import_id,)
    ).fetchone()
    assert tuple(stored) == ("uploaded", "rejected")


def test_collector_queue_bounds_pending_storage_and_prunes_old_terminal_rows(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(collector_core, "OUTBOX_PENDING_BATCH_LIMIT", 1)
    queue = CollectorQueue(tmp_path / "collector.sqlite3")
    first = queue.stage(_payload("123456"))

    with pytest.raises(CollectorQueueFull, match="outbox is full"):
        queue.stage(_payload("234567"))

    class AcceptingTransport:
        def upload(self, *, import_id, payload, token):
            return UploadReceipt(
                import_id=import_id,
                payload_sha256=hashlib.sha256(payload).hexdigest(),
                state="accepted",
            )

    assert queue.retry_pending(transport=AcceptingTransport(), token="device-token").uploaded == 1
    queue.stage(
        _payload("234567"),
        now=datetime.now(UTC) + timedelta(days=31),
    )

    assert queue.connection().execute(
        "SELECT count(*) FROM batches WHERE import_id = ?", (first.import_id,)
    ).fetchone()[0] == 0


def test_upload_transport_targets_only_the_fixed_https_import_endpoint():
    captured = {}
    payload = b"{}"
    payload_sha256 = hashlib.sha256(payload).hexdigest()

    class Response:
        status = 202

        def __init__(self):
            self.headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit):
            return json.dumps(
                {
                    "import_id": "a" * 32,
                    "payload_sha256": payload_sha256,
                    "state": "pending",
                }
            ).encode()

    class Opener:
        def open(self, request, timeout):
            captured["url"] = request.full_url
            captured["method"] = request.method
            captured["headers"] = dict(request.header_items())
            captured["body"] = request.data
            captured["timeout"] = timeout
            return Response()

    receipt = GovScoutUploadTransport(opener=Opener()).upload(
        import_id="a" * 32,
        payload=payload,
        token="gsc_" + "c" * 32 + "_" + "d" * 43,
    )

    assert captured["url"] == "https://leads.misegroup.co.uk/api/v1/collector/imports"
    assert captured["method"] == "POST"
    assert captured["headers"]["Authorization"].startswith("Bearer gsc_")
    assert captured["headers"]["Idempotency-key"] == "a" * 32
    assert captured["body"] == b"{}"
    assert receipt.state == "pending"


def test_a_completed_identical_snapshot_can_be_staged_as_a_new_observation(tmp_path):
    queue = CollectorQueue(tmp_path / "collector.sqlite3")
    first = queue.stage(_payload())

    class Transport:
        def upload(self, *, import_id, payload, token):
            return UploadReceipt(
                import_id=import_id,
                payload_sha256=hashlib.sha256(payload).hexdigest(),
                state="accepted",
            )

    assert queue.retry_pending(transport=Transport(), token="device-token").uploaded == 1
    second = queue.stage(_payload())

    assert second.import_id != first.import_id
