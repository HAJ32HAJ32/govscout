from datetime import UTC, datetime
import json

import pytest

from govscout.db import connect_database, migrate
from govscout.fca_discovery import (
    FcaDataError,
    FcaFirmRecord,
    ingest_fca_records,
    parse_fca_json,
)


def _payload(*firms):
    return json.dumps({"firms": list(firms)}).encode()


def _firm(frn="123456", **changes):
    record = {
        "frn": frn,
        "firm_name": f"Example {frn} Finance Ltd",
        "status": "Authorised",
        "firm_type": "Regulated firm",
        "source_url": f"https://register.fca.org.uk/s/firm?id={frn}",
        "website_url": f"https://example-{frn}.test/",
        "location": "London",
        "company_number": f"{int(frn):08d}",
    }
    record.update(changes)
    return record


def test_fca_discovery_stages_bounded_authoritative_records_idempotently(tmp_path):
    records = parse_fca_json(_payload(_firm("123456"), _firm("234567"), _firm("345678")))
    assert records[0] == FcaFirmRecord(
        frn="123456",
        firm_name="Example 123456 Finance Ltd",
        fca_status="Authorised",
        firm_type="Regulated firm",
        source_url="https://register.fca.org.uk/s/firm?id=123456",
        website_url="https://example-123456.test/",
        source_location="London",
        company_number="00123456",
        is_active=True,
    )
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    now = datetime(2026, 7, 25, 10, 0, tzinfo=UTC)

    first = ingest_fca_records(conn, records, limit=2, now=now)
    second = ingest_fca_records(conn, records, limit=2, now=now)

    assert (first.source_count, first.staged_count, first.created_count) == (3, 2, 2)
    assert second.created_count == 0
    assert conn.execute("SELECT count(*) FROM fca_firms").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM fca_observations").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM leads").fetchone()[0] == 0


def test_fca_discovery_records_changed_evidence_without_rewriting_history(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    first = parse_fca_json(_payload(_firm(location="London")))
    changed = parse_fca_json(_payload(_firm(location="Bristol")))

    ingest_fca_records(conn, first, limit=1, now=datetime(2026, 7, 25, 10, tzinfo=UTC))
    result = ingest_fca_records(
        conn, changed, limit=1, now=datetime(2026, 7, 26, 10, tzinfo=UTC)
    )

    assert result.changed_count == 1
    assert conn.execute("SELECT count(*) FROM fca_observations").fetchone()[0] == 2
    assert conn.execute("SELECT source_location FROM fca_firms").fetchone()[0] == "Bristol"


@pytest.mark.parametrize(
    "record",
    [
        _firm(frn="123"),
        _firm(source_url="https://attacker.example/s/firm?id=123456"),
        _firm(website_url="http://example.test/"),
        _firm(company_number="123"),
        _firm(status=""),
    ],
)
def test_fca_discovery_rejects_malformed_or_non_authoritative_records(record):
    with pytest.raises(FcaDataError):
        parse_fca_json(_payload(record))


def test_fca_discovery_rejects_oversized_or_duplicate_data():
    with pytest.raises(FcaDataError, match="exceeded"):
        parse_fca_json(b"x" * 1_000_001)
    with pytest.raises(FcaDataError, match="duplicate FRN"):
        parse_fca_json(_payload(_firm(), _firm()))
