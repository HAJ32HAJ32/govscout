import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from govscout.auth import create_collector_device
from govscout.collector_imports import process_collector_import
from govscout.db import connect_database, migrate
from govscout.fca_discovery import FcaDataError, ingest_fca_records, parse_fca_json

NOW = datetime(2026, 7, 30, 10, tzinfo=UTC)


def _payload():
    return json.dumps(
        {
            "firms": [
                {
                    "frn": "123456",
                    "firm_name": "Example Finance Ltd",
                    "status": "Authorised",
                    "firm_type": "Regulated firm",
                    "source_url": "https://register.fca.org.uk/s/firm?id=123456",
                    "website_url": "https://example.test/",
                    "location": "London",
                    "company_number": "12345678",
                }
            ]
        },
        separators=(",", ":"),
    )


def test_vps_processing_ingests_a_staged_import_once_without_creating_leads(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    credential = create_collector_device(conn, display_name="H Windows PC", now=NOW)
    payload = _payload()
    conn.execute(
        """
        INSERT INTO collector_imports (
            import_id, device_id, payload_sha256, payload_json, state, received_at
        ) VALUES (?, ?, ?, ?, 'pending', ?)
        """,
        (
            "a" * 32,
            credential.device_id,
            hashlib.sha256(payload.encode()).hexdigest(),
            payload,
            NOW.isoformat(),
        ),
    )

    first = process_collector_import(
        conn,
        import_id="a" * 32,
        now=NOW + timedelta(seconds=1),
    )
    second = process_collector_import(
        conn,
        import_id="a" * 32,
        now=NOW + timedelta(seconds=2),
    )

    assert first == second
    assert first.state == "accepted"
    assert first.source_count == 1
    assert first.staged_count == 1
    assert first.created_count == 1
    assert conn.execute("SELECT count(*) FROM fca_firms").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM leads").fetchone()[0] == 0
    stored = conn.execute(
        "SELECT state, result_json, error_code FROM collector_imports"
    ).fetchone()
    assert stored["state"] == "accepted"
    assert json.loads(stored["result_json"])["created_count"] == 1
    assert stored["error_code"] is None


def test_rejected_multi_firm_import_rolls_back_records_written_before_stale_record(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    credential = create_collector_device(conn, display_name="H Windows PC", now=NOW)
    future_payload = json.dumps(
        {
            "firms": [
                {
                    "frn": "200000",
                    "firm_name": "Existing Finance Ltd",
                    "status": "Authorised",
                    "firm_type": "Regulated firm",
                    "source_url": "https://register.fca.org.uk/s/firm?id=200000",
                    "website_url": None,
                    "location": "London",
                    "company_number": "20000000",
                }
            ]
        },
        separators=(",", ":"),
    ).encode()
    ingest_fca_records(
        conn,
        parse_fca_json(future_payload),
        limit=25,
        now=NOW + timedelta(hours=1),
    )
    payload = json.dumps(
        {
            "firms": [
                {
                    "frn": "100000",
                    "firm_name": "Must Roll Back Ltd",
                    "status": "Authorised",
                    "firm_type": "Regulated firm",
                    "source_url": "https://register.fca.org.uk/s/firm?id=100000",
                    "website_url": None,
                    "location": "London",
                    "company_number": "10000000",
                },
                {
                    "frn": "200000",
                    "firm_name": "Stale Update Ltd",
                    "status": "Authorised",
                    "firm_type": "Regulated firm",
                    "source_url": "https://register.fca.org.uk/s/firm?id=200000",
                    "website_url": None,
                    "location": "London",
                    "company_number": "20000000",
                },
            ]
        },
        separators=(",", ":"),
    )
    conn.execute(
        """
        INSERT INTO collector_imports (
            import_id, device_id, payload_sha256, payload_json, state, received_at
        ) VALUES (?, ?, ?, ?, 'pending', ?)
        """,
        (
            "b" * 32,
            credential.device_id,
            hashlib.sha256(payload.encode()).hexdigest(),
            payload,
            NOW.isoformat(),
        ),
    )

    result = process_collector_import(conn, import_id="b" * 32, now=NOW)

    assert result.state == "rejected"
    assert result.error_code == "FCA_DATA_REJECTED"
    assert conn.execute(
        "SELECT count(*) FROM fca_firms WHERE frn = '100000'"
    ).fetchone()[0] == 0
    existing = conn.execute(
        "SELECT firm_name FROM fca_firms WHERE frn = '200000'"
    ).fetchone()
    assert existing["firm_name"] == "Existing Finance Ltd"


def test_fca_parser_rejects_a_record_too_large_for_immutable_observation_storage():
    payload = json.dumps(
        {
            "firms": [
                {
                    "frn": "123456",
                    "firm_name": "X" * 40_000,
                    "status": "Authorised",
                    "firm_type": None,
                    "source_url": "https://register.fca.org.uk/s/firm?id=123456",
                    "website_url": None,
                    "location": None,
                    "company_number": None,
                }
            ]
        },
        separators=(",", ":"),
    ).encode()

    with pytest.raises(FcaDataError, match="immutable observation limit"):
        parse_fca_json(payload)
