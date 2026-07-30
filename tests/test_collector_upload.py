import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

from govscout.auth import (
    AuthConfig,
    create_collector_device,
    hash_password,
    revoke_collector_device,
)
from govscout.config import load_settings
from govscout.db import connect_database, migrate
from govscout.sendguard import SendGuard
from govscout.web.app import create_app

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_HOST = "leads.misegroup.co.uk"
BASE_URL = f"https://{PUBLIC_HOST}"
NOW = datetime(2026, 7, 30, 10, tzinfo=UTC)


def _payload(*, frn="123456", company_number="12345678") -> bytes:
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
                    "company_number": company_number,
                }
            ]
        },
        separators=(",", ":"),
    ).encode()


def _collector_app(tmp_path):
    database = tmp_path / "govscout.sqlite3"
    conn = connect_database(database)
    migrate(conn)
    credential = create_collector_device(conn, display_name="H Windows PC", now=NOW)
    conn.close()
    auth = AuthConfig(
        username="operator",
        password_hash=hash_password("test-password", salt=b"0123456789abcdef"),
        session_secret=b"s" * 32,
        public_host=PUBLIC_HOST,
        public_https=True,
    )
    app = create_app(
        conn_factory=lambda: connect_database(database),
        guard=SendGuard(load_settings(ROOT / "config/default.toml")),
        now_provider=lambda: NOW,
        auth=auth,
    )
    app.testing = True
    return app, database, credential


def _many_firms_payload(count):
    return json.dumps(
        {
            "firms": [
                {
                    "frn": f"{123456 + index}",
                    "firm_name": f"Firm {index} Ltd",
                    "status": "Authorised",
                    "firm_type": "Regulated firm",
                    "source_url": (
                        "https://register.fca.org.uk/s/firm?id=" f"{123456 + index}"
                    ),
                    "website_url": None,
                    "location": "London",
                    "company_number": f"{12345678 + index:08d}",
                }
                for index in range(count)
            ]
        },
        separators=(",", ":"),
    ).encode()


def test_authenticated_collector_upload_is_durable_and_idempotent(tmp_path):
    app, database, credential = _collector_app(tmp_path)
    client = app.test_client()
    payload = _payload()
    import_id = "a" * 32
    payload_sha256 = hashlib.sha256(payload).hexdigest()
    headers = {
        "Authorization": f"Bearer {credential.token}",
        "Content-Type": "application/json",
        "Idempotency-Key": import_id,
        "X-Payload-SHA256": payload_sha256,
    }

    accepted = client.post(
        "/api/v1/collector/imports",
        base_url=BASE_URL,
        headers=headers,
        data=payload,
    )
    retry = client.post(
        "/api/v1/collector/imports",
        base_url=BASE_URL,
        headers=headers,
        data=payload,
    )

    expected = {
        "import_id": import_id,
        "payload_sha256": payload_sha256,
        "state": "accepted",
    }
    assert accepted.status_code == 202
    assert accepted.get_json() == expected
    assert retry.status_code == 200
    assert retry.get_json() == expected
    verify = connect_database(database)
    row = verify.execute(
        """
        SELECT device_id, payload_sha256, payload_json, state
        FROM collector_imports
        """
    ).fetchone()
    assert tuple(row) == (
        credential.device_id,
        payload_sha256,
        payload.decode(),
        "accepted",
    )
    assert verify.execute("SELECT count(*) FROM collector_imports").fetchone()[0] == 1
    assert verify.execute("SELECT count(*) FROM fca_firms").fetchone()[0] == 1
    assert verify.execute("SELECT count(*) FROM leads").fetchone()[0] == 0


def test_collector_upload_authenticates_before_exposing_payload_validation(tmp_path):
    app, database, _credential = _collector_app(tmp_path)

    response = app.test_client().post(
        "/api/v1/collector/imports",
        base_url=BASE_URL,
        headers={"Authorization": "Bearer not-a-device-token"},
        data=b"not json",
    )

    assert response.status_code == 401
    assert response.get_json() == {"error": "collector_unauthorized"}
    assert "Set-Cookie" not in response.headers
    verify = connect_database(database)
    assert verify.execute("SELECT count(*) FROM collector_imports").fetchone()[0] == 0


def test_collector_upload_rejects_idempotency_key_reuse_for_another_payload(tmp_path):
    app, database, credential = _collector_app(tmp_path)
    client = app.test_client()
    import_id = "a" * 32

    def upload(payload):
        return client.post(
            "/api/v1/collector/imports",
            base_url=BASE_URL,
            headers={
                "Authorization": f"Bearer {credential.token}",
                "Content-Type": "application/json",
                "Idempotency-Key": import_id,
                "X-Payload-SHA256": hashlib.sha256(payload).hexdigest(),
            },
            data=payload,
        )

    assert upload(_payload()).status_code == 202
    conflict = upload(_payload(frn="654321", company_number="87654321"))

    assert conflict.status_code == 409
    assert conflict.get_json() == {"error": "collector_import_conflict"}
    verify = connect_database(database)
    assert verify.execute("SELECT count(*) FROM collector_imports").fetchone()[0] == 1


def test_collector_upload_has_a_dedicated_bounded_payload_limit(tmp_path):
    app, database, credential = _collector_app(tmp_path)
    oversized = b"x" * 1_000_001

    response = app.test_client().post(
        "/api/v1/collector/imports",
        base_url=BASE_URL,
        headers={
            "Authorization": f"Bearer {credential.token}",
            "Content-Type": "application/json",
            "Idempotency-Key": "a" * 32,
            "X-Payload-SHA256": hashlib.sha256(oversized).hexdigest(),
        },
        data=oversized,
    )

    assert response.status_code == 413
    verify = connect_database(database)
    assert verify.execute("SELECT count(*) FROM collector_imports").fetchone()[0] == 0


def test_collector_upload_rejects_more_than_the_atomic_batch_limit(tmp_path):
    app, database, credential = _collector_app(tmp_path)
    payload = _many_firms_payload(26)

    response = app.test_client().post(
        "/api/v1/collector/imports",
        base_url=BASE_URL,
        headers={
            "Authorization": f"Bearer {credential.token}",
            "Content-Type": "application/json",
            "Idempotency-Key": "a" * 32,
            "X-Payload-SHA256": hashlib.sha256(payload).hexdigest(),
        },
        data=payload,
    )

    assert response.status_code == 422
    assert response.get_json() == {"error": "collector_batch_limit_exceeded"}
    verify = connect_database(database)
    assert verify.execute("SELECT count(*) FROM collector_imports").fetchone()[0] == 0


def test_same_payload_with_a_new_import_id_is_a_fresh_observation(tmp_path):
    app, database, credential = _collector_app(tmp_path)
    client = app.test_client()
    payload = _payload()

    for import_id in ("a" * 32, "b" * 32):
        response = client.post(
            "/api/v1/collector/imports",
            base_url=BASE_URL,
            headers={
                "Authorization": f"Bearer {credential.token}",
                "Content-Type": "application/json",
                "Idempotency-Key": import_id,
                "X-Payload-SHA256": hashlib.sha256(payload).hexdigest(),
            },
            data=payload,
        )
        assert response.status_code == 202

    verify = connect_database(database)
    assert verify.execute("SELECT count(*) FROM collector_imports").fetchone()[0] == 2


def test_collector_upload_rate_limit_is_persistent_per_device(tmp_path):
    app, _database, credential = _collector_app(tmp_path)
    client = app.test_client()

    for number in range(12):
        response = client.post(
            "/api/v1/collector/imports",
            base_url=BASE_URL,
            headers={
                "Authorization": f"Bearer {credential.token}",
                "Idempotency-Key": f"{number:032x}",
                "X-Payload-SHA256": "0" * 64,
            },
        )
        assert response.status_code == 415

    limited = app.test_client().post(
        "/api/v1/collector/imports",
        base_url=BASE_URL,
        headers={
            "Authorization": f"Bearer {credential.token}",
            "Idempotency-Key": "f" * 32,
            "X-Payload-SHA256": "0" * 64,
        },
    )

    assert limited.status_code == 429
    assert limited.get_json() == {"error": "collector_rate_limited"}


def test_collector_upload_refuses_new_storage_after_device_import_cap(tmp_path):
    app, database, credential = _collector_app(tmp_path)
    payload = _payload()
    payload_sha256 = hashlib.sha256(payload).hexdigest()
    conn = connect_database(database)
    for number in range(100):
        conn.execute(
            """
            INSERT INTO collector_imports (
                import_id, device_id, payload_sha256, payload_json,
                state, received_at
            ) VALUES (?, ?, ?, ?, 'pending', ?)
            """,
            (f"{number:032x}", credential.device_id, payload_sha256, payload.decode(), NOW.isoformat()),
        )
    conn.close()

    response = app.test_client().post(
        "/api/v1/collector/imports",
        base_url=BASE_URL,
        headers={
            "Authorization": f"Bearer {credential.token}",
            "Content-Type": "application/json",
            "Idempotency-Key": "f" * 32,
            "X-Payload-SHA256": payload_sha256,
        },
        data=payload,
    )

    assert response.status_code == 429
    assert response.get_json() == {"error": "collector_storage_limit"}


def test_revocation_between_request_admission_and_import_write_blocks_new_import(
    tmp_path, monkeypatch
):
    import govscout.web.app as web_app_module

    app, database, credential = _collector_app(tmp_path)
    payload = _payload()
    real_parse_fca_json = web_app_module.parse_fca_json

    def revoke_then_parse(candidate_payload):
        conn = connect_database(database)
        try:
            revoke_collector_device(
                conn,
                device_id=credential.device_id,
                now=NOW + timedelta(seconds=1),
            )
        finally:
            conn.close()
        return real_parse_fca_json(candidate_payload)

    monkeypatch.setattr(web_app_module, "parse_fca_json", revoke_then_parse)

    response = app.test_client().post(
        "/api/v1/collector/imports",
        base_url=BASE_URL,
        headers={
            "Authorization": f"Bearer {credential.token}",
            "Content-Type": "application/json",
            "Idempotency-Key": "f" * 32,
            "X-Payload-SHA256": hashlib.sha256(payload).hexdigest(),
        },
        data=payload,
    )

    assert response.status_code == 401
    assert response.get_json() == {"error": "collector_unauthorized"}
    verify = connect_database(database)
    assert verify.execute("SELECT count(*) FROM collector_imports").fetchone()[0] == 0


def test_concurrent_same_key_uploads_create_one_import_and_one_observation(tmp_path):
    app, database, credential = _collector_app(tmp_path)
    payload = _payload()
    headers = {
        "Authorization": f"Bearer {credential.token}",
        "Content-Type": "application/json",
        "Idempotency-Key": "d" * 32,
        "X-Payload-SHA256": hashlib.sha256(payload).hexdigest(),
    }

    def upload():
        return app.test_client().post(
            "/api/v1/collector/imports",
            base_url=BASE_URL,
            headers=headers,
            data=payload,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _index: upload(), range(2)))

    assert sorted(response.status_code for response in responses) == [200, 202]
    verify = connect_database(database)
    assert verify.execute("SELECT count(*) FROM collector_imports").fetchone()[0] == 1
    assert verify.execute("SELECT count(*) FROM fca_observations").fetchone()[0] == 1
