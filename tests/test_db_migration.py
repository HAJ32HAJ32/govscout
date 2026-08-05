import hashlib
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from govscout.companies_house import VerifiedCompany
from govscout.db import (
    ALLOWED_LEGAL_FORMS,
    _migration_texts,
    _statements,
    connect_database,
    insert_verified_lead,
    migrate,
)
from govscout.quality import company_verification_is_current
from tests.support import (
    verified_company_from_test_profile as verified_company_from_profile,
)

ROOT = Path(__file__).resolve().parents[1]


def test_runbook_requires_database_restore_when_rolling_back_migration_013():
    runbook = (ROOT / "deploy/production/v1/RUNBOOK.md").read_text(encoding="utf-8")
    assert "Migration 013 adds append-only archive and restore state" in runbook
    rollback = runbook.split("## 6. Rollback", maxsplit=1)[1]
    assert "migration 013" in rollback
    assert "restore the verified pre-release backup" in rollback
    assert "Pre-013 code does not enforce archive state" in rollback


def test_concurrent_first_database_connections_do_not_race(tmp_path, monkeypatch):
    database = tmp_path / "first-start.sqlite3"
    barrier = threading.Barrier(2)
    original_exists = Path.exists

    def synchronised_exists(path):
        if path == database:
            barrier.wait(timeout=5)
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", synchronised_exists)

    def connect_and_close():
        conn = connect_database(database)
        conn.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(connect_and_close) for _ in range(2)]
    errors = [future.exception() for future in futures]
    assert errors == [None, None]


def _verified(number="12345678", name="Example Governance Ltd"):
    return verified_company_from_profile(
        {
            "company_number": number,
            "company_name": name,
            "company_status": "active",
            "type": "ltd",
        },
        now=datetime(2026, 7, 20, 9, 0, tzinfo=UTC),
    )


def _lead(conn, number="12345678"):
    return insert_verified_lead(
        conn,
        company=_verified(number, f"Example {number} Ltd"),
        contact_email=f"director-{number}@example.test",
        source_register="LCA member directory",
    )


def _migrate_until(conn, stop_version):
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        """
        CREATE TABLE schema_migrations (
            version TEXT PRIMARY KEY,
            checksum TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    for version, _name, sql in _migration_texts():
        if version == stop_version:
            break
        for statement in _statements(sql):
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_migrations (version, checksum, applied_at) VALUES (?, ?, ?)",
            (
                version,
                hashlib.sha256(sql.encode("utf-8")).hexdigest(),
                "2026-07-29T09:00:00+00:00",
            ),
        )
    conn.execute("COMMIT")


def _migrate_through_007(conn):
    _migrate_until(conn, "008")


def _migrate_through_009(conn):
    _migrate_until(conn, "010")


def _insert_send_in_state(conn, lead_id, state):
    values = {
        "reserved": (None, None, None, None, None, None),
        "draft": (
            "2026-07-20T08:01:00+00:00",
            None,
            None,
            None,
            "draft-1",
            None,
        ),
        "sent": (
            "2026-07-20T08:01:00+00:00",
            "2026-07-20T08:02:00+00:00",
            None,
            None,
            "draft-1",
            "message-1",
        ),
        "void": (
            "2026-07-20T08:01:00+00:00",
            None,
            "2026-07-20T08:02:00+00:00",
            None,
            "draft-1",
            None,
        ),
        "failed": (
            None,
            None,
            None,
            "2026-07-20T08:02:00+00:00",
            None,
            None,
        ),
    }
    drafted_at, sent_at, voided_at, failed_at, draft_id, message_id = values[state]
    failure_reason = "terminal failure" if state == "failed" else None
    return conn.execute(
        """
        INSERT INTO sends (
            lead_id, to_email, stage, template, subject, body_hash,
            word_count, state, created_at, drafted_at, sent_at, voided_at,
            failed_at, failure_reason, gmail_draft_id, gmail_message_id
        ) VALUES (?, ?, 0, 'signal-led', 'subject', ?, 10, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            lead_id,
            "director-12345678@example.test",
            "a" * 64,
            state,
            "2026-07-20T08:00:00+00:00",
            drafted_at,
            sent_at,
            voided_at,
            failed_at,
            failure_reason,
            draft_id,
            message_id,
        ),
    ).lastrowid


def test_migration_is_versioned_idempotent_and_creates_p1_tables(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")

    migrate(conn)
    migrate(conn)

    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {
        "schema_migrations",
        "app_state",
        "candidates",
        "fca_firms",
        "fca_observations",
        "enrichment_runs",
        "evidence_items",
        "qc_runs",
        "firm_reviews",
        "collector_devices",
        "collector_imports",
        "company_verification_attempts",
        "fca_processing_jobs",
        "fca_processing_job_events",
        "firm_archive_events",
        "retirement_events",
        "leads",
        "sends",
    }.issubset(tables)
    migrations = conn.execute(
        "SELECT version, length(checksum) FROM schema_migrations ORDER BY version"
    ).fetchall()
    assert [tuple(row) for row in migrations] == [
        ("001", 64),
        ("002", 64),
        ("003", 64),
        ("004", 64),
        ("005", 64),
        ("006", 64),
        ("007", 64),
        ("008", 64),
        ("009", 64),
        ("010", 64),
        ("011", 64),
        ("012", 64),
        ("013", 64),
    ]


def test_migration_013_upgrades_populated_review_history(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    _migrate_until(conn, "013")
    firm_id = conn.execute(
        """
        INSERT INTO fca_firms (
            frn, firm_name, fca_status, is_active, source_url,
            source_record_hash, first_seen_at, last_seen_at
        ) VALUES ('123456', 'Existing Review Ltd', 'Authorised', 1, ?, ?, ?, ?)
        """,
        (
            "https://register.fca.org.uk/s/firm?id=123456",
            "a" * 64,
            "2026-08-05T08:00:00+00:00",
            "2026-08-05T08:00:00+00:00",
        ),
    ).lastrowid
    review_id = conn.execute(
        """
        INSERT INTO firm_reviews (
            firm_id, decision, qc_run_id, notes, rejection_reason, reviewed_at
        ) VALUES (?, 'rejected', NULL, 'Existing note', 'Not suitable', ?)
        """,
        (firm_id, "2026-08-05T08:01:00+00:00"),
    ).lastrowid

    migrate(conn)

    assert conn.execute(
        "SELECT archive_event_id FROM firm_reviews WHERE id = ?",
        (review_id,),
    ).fetchone()[0] is None
    assert conn.execute(
        "SELECT count(*) FROM firm_archive_events"
    ).fetchone()[0] == 0


def test_archive_event_schema_enforces_legal_append_only_transitions(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    firm_id = conn.execute(
        """
        INSERT INTO fca_firms (
            frn, firm_name, fca_status, is_active, source_url,
            source_record_hash, first_seen_at, last_seen_at
        ) VALUES ('123456', 'Archive Test Ltd', 'Authorised', 1, ?, ?, ?, ?)
        """,
        (
            "https://register.fca.org.uk/s/firm?id=123456",
            "a" * 64,
            "2026-08-05T08:00:00+00:00",
            "2026-08-05T08:00:00+00:00",
        ),
    ).lastrowid
    invalid_events = (
        ("\t", "test-operator", "2026-08-05T08:01:00+00:00"),
        ("Valid reason", "\n", "2026-08-05T08:01:00+00:00"),
        ("Valid\x00hidden", "test-operator", "2026-08-05T08:01:00+00:00"),
        ("Valid reason", "operator\x00hidden", "2026-08-05T08:01:00+00:00"),
        (
            "Valid reason",
            "test-operator",
            "2026-08-05T08:01:00+00:00\x00hidden",
        ),
        ("Valid reason", "test-operator", "not-a-timestamp+00:00"),
        ("Valid reason", "test-operator", "2026-08-05T24:00:00+00:00"),
    )
    for reason, actor, occurred_at in invalid_events:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO firm_archive_events (
                    firm_id, action, reason, actor,
                    expected_previous_event_id, occurred_at
                ) VALUES (?, 'archive', ?, ?, NULL, ?)
                """,
                (firm_id, reason, actor, occurred_at),
            )
    with pytest.raises(sqlite3.IntegrityError, match="not archived"):
        conn.execute(
            """
            INSERT INTO firm_archive_events (
                firm_id, action, reason, actor, expected_previous_event_id, occurred_at
            ) VALUES (?, 'restore', 'Invalid first action', 'test-operator', NULL, ?)
            """,
            (firm_id, "2026-08-05T08:01:00+00:00"),
        )
    archive_id = conn.execute(
        """
        INSERT INTO firm_archive_events (
            firm_id, action, reason, actor, expected_previous_event_id, occurred_at
        ) VALUES (?, 'archive', 'Outside target market', 'test-operator', NULL, ?)
        """,
        (firm_id, "2026-08-05T08:02:00+00:00"),
    ).lastrowid
    with pytest.raises(sqlite3.IntegrityError, match="stale archive event"):
        conn.execute(
            """
            INSERT INTO firm_archive_events (
                firm_id, action, reason, actor, expected_previous_event_id, occurred_at
            ) VALUES (?, 'restore', 'Stale browser state', 'test-operator', NULL, ?)
            """,
            (firm_id, "2026-08-05T08:03:00+00:00"),
        )
    with pytest.raises(sqlite3.IntegrityError, match="already archived"):
        conn.execute(
            """
            INSERT INTO firm_archive_events (
                firm_id, action, reason, actor, expected_previous_event_id, occurred_at
            ) VALUES (?, 'archive', 'Duplicate action', 'test-operator', ?, ?)
            """,
            (firm_id, archive_id, "2026-08-05T08:03:00+00:00"),
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "UPDATE firm_archive_events SET reason = 'Rewritten' WHERE id = ?",
            (archive_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("DELETE FROM firm_archive_events WHERE id = ?", (archive_id,))


def test_company_verification_attempts_are_append_only_and_fail_closed(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    firm_id = conn.execute(
        """
        INSERT INTO fca_firms (
            frn, firm_name, fca_status, is_active, source_url, company_number,
            source_record_hash, first_seen_at, last_seen_at
        ) VALUES ('123456', 'Example Ltd', 'Authorised', 1, ?, '12345678', ?, ?, ?)
        """,
        (
            "https://register.fca.org.uk/s/firm?id=123456",
            "a" * 64,
            "2026-08-05T08:00:00+00:00",
            "2026-08-05T08:00:00+00:00",
        ),
    ).lastrowid
    attempt_id = conn.execute(
        """
        INSERT INTO company_verification_attempts (
            firm_id, company_number, state, reason_code, checked_at,
            fca_source_record_hash, legal_name, legal_form, company_status,
            profile_hash
        ) VALUES (?, '12345678', 'verified', 'VERIFIED', ?, ?,
            'Example Ltd', 'ltd', 'active', ?)
        """,
        (
            firm_id,
            "2026-08-05T08:01:00+00:00",
            "a" * 64,
            "b" * 64,
        ),
    ).lastrowid
    run_id = conn.execute(
        """
        INSERT INTO enrichment_runs (
            firm_id, state, started_at, completed_at, website_url, final_url,
            input_hash, page_hash, score, temperature
        ) VALUES (?, 'complete', ?, ?, 'https://example.test/',
                  'https://example.test/', ?, ?, 0, 'COOL')
        """,
        (
            firm_id,
            "2026-08-05T08:01:00+00:00",
            "2026-08-05T08:02:00+00:00",
            "c" * 64,
            "d" * 64,
        ),
    ).lastrowid

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE company_verification_attempts SET reason_code = 'CHANGED' WHERE id = ?",
            (attempt_id,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM company_verification_attempts WHERE id = ?", (attempt_id,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO company_verification_attempts (
                firm_id, company_number, state, reason_code, checked_at,
                fca_source_record_hash
            ) VALUES (?, '12345678', 'verified', 'VERIFIED', ?, ?)
            """,
            (
                firm_id,
                "2026-08-05T08:02:00+00:00",
                "a" * 64,
            ),
        )
    with pytest.raises(sqlite3.IntegrityError, match="verification attempt"):
        conn.execute(
            """
            INSERT INTO qc_runs (
                firm_id, enrichment_run_id, state, reason_codes, input_hash,
                checked_at, expires_at
            ) VALUES (?, ?, 'pass', '[]', ?, ?, ?)
            """,
            (
                firm_id,
                run_id,
                "e" * 64,
                "2026-08-05T08:02:00+00:00",
                "2026-08-06T08:02:00+00:00",
            ),
        )


def test_migration_010_backfills_prior_verified_lead_with_thirty_day_freshness(
    tmp_path,
):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    _migrate_through_009(conn)
    lead_id = _lead(conn)
    firm_id = conn.execute(
        """
        INSERT INTO fca_firms (
            frn, firm_name, fca_status, firm_type, is_active, source_url,
            website_url, source_location, company_number, source_record_hash,
            first_seen_at, last_seen_at, lead_id
        ) VALUES ('123456', 'Example 12345678 Ltd', 'Authorised',
                  'Regulated firm', 1,
                  'https://register.fca.org.uk/s/firm?id=123456',
                  'https://example.test/', 'London', '12345678', ?, ?, ?, ?)
        """,
        (
            "a" * 64,
            "2026-07-20T09:00:00+00:00",
            "2026-07-20T09:00:00+00:00",
            lead_id,
        ),
    ).lastrowid
    assert firm_id is not None

    migrate(conn)

    attempt = conn.execute(
        """
        SELECT state, reason_code, checked_at, fca_source_record_hash
        FROM company_verification_attempts WHERE firm_id = ?
        """,
        (firm_id,),
    ).fetchone()
    assert tuple(attempt) == (
        "verified",
        "VERIFIED",
        "2026-07-20T09:00:00+00:00",
        "a" * 64,
    )
    assert company_verification_is_current(
        conn, firm_id=firm_id, now=datetime(2026, 8, 5, 9, tzinfo=UTC)
    )
    assert not company_verification_is_current(
        conn, firm_id=firm_id, now=datetime(2026, 8, 20, 9, tzinfo=UTC)
    )


def test_collector_schema_keeps_device_secrets_hashed_and_imports_durable(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)

    conn.execute(
        """
        INSERT INTO collector_devices (
            device_id, display_name, token_hash, created_at
        ) VALUES (?, ?, ?, ?)
        """,
        (
            "a" * 32,
            "H Windows PC",
            "b" * 64,
            "2026-07-30T10:00:00+00:00",
        ),
    )
    conn.execute(
        """
        INSERT INTO collector_imports (
            import_id, device_id, payload_sha256, payload_json,
            state, received_at
        ) VALUES (?, ?, ?, ?, 'pending', ?)
        """,
        (
            "c" * 32,
            "a" * 32,
            "d" * 64,
            '{"firms":[]}',
            "2026-07-30T10:01:00+00:00",
        ),
    )

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM collector_imports")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE collector_imports SET payload_json = '{}' ")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM collector_devices")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE collector_devices SET token_hash = ?", ("e" * 64,))


def test_fca_schema_fails_closed_on_invalid_identity_and_unevidenced_signal(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO fca_firms (
                frn, firm_name, fca_status, is_active, source_url,
                source_record_hash, first_seen_at, last_seen_at
            ) VALUES ('not-an-frn', 'Example', 'Authorised', 1, ?, ?, ?, ?)
            """,
            (
                "https://register.fca.org.uk/s/firm?id=001",
                "a" * 64,
                "2026-07-25T10:00:00+00:00",
                "2026-07-25T10:00:00+00:00",
            ),
        )

    firm_id = conn.execute(
        """
        INSERT INTO fca_firms (
            frn, firm_name, fca_status, is_active, source_url,
            source_record_hash, first_seen_at, last_seen_at
        ) VALUES ('123456', 'Example Finance Ltd', 'Authorised', 1, ?, ?, ?, ?)
        """,
        (
            "https://register.fca.org.uk/s/firm?id=123456",
            "a" * 64,
            "2026-07-25T10:00:00+00:00",
            "2026-07-25T10:00:00+00:00",
        ),
    ).lastrowid
    run_id = conn.execute(
        """
        INSERT INTO enrichment_runs (
            firm_id, state, started_at, completed_at, input_hash, score, temperature
        ) VALUES (?, 'complete', ?, ?, ?, 0, 'COOL')
        """,
        (
            firm_id,
            "2026-07-25T10:00:00+00:00",
            "2026-07-25T10:01:00+00:00",
            "c" * 64,
        ),
    ).lastrowid

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO evidence_items (
                run_id, signal_group, code, evidence_state, weight,
                source_url, excerpt, observed_at, content_hash
            ) VALUES (?, 'ai_exposure', 'AI_CLAIM', 'present', 20, NULL, NULL, ?, ?)
            """,
            (run_id, "2026-07-25T10:00:00+00:00", "b" * 64),
        )


@pytest.mark.parametrize(
    "source_url",
    [
        "https://register.fca.org.uk/s/firm?id=654321",
        "https://register.fca.org.uk/s/firm?id=123456&extra=1",
        "https://register.fca.org.uk/s/firm/123456",
    ],
)
def test_fca_source_url_must_exactly_match_its_frn(tmp_path, source_url):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO fca_firms (
                frn, firm_name, fca_status, is_active, source_url,
                source_record_hash, first_seen_at, last_seen_at
            ) VALUES ('123456', 'Example', 'Authorised', 1, ?, ?, ?, ?)
            """,
            (
                source_url,
                "a" * 64,
                "2026-07-25T10:00:00+00:00",
                "2026-07-25T10:00:00+00:00",
            ),
        )


@pytest.mark.parametrize(
    "website_url",
    [
        "https://",
        "https://user@example.com/",
        "https://example.com/#fragment",
        "https://example.com/ bad",
        "https://example.com/\nadmin",
        "https:///missing-host",
        "https://example.com:443/",
        "https://example.com:8443/",
        "https://-bad.example/",
        "https://bad-.example/",
        "https://bad..example/",
        "https://.bad.example/",
        "https://bad.example./",
    ],
)
def test_fca_website_url_rejects_ambiguous_or_unsafe_direct_sql(tmp_path, website_url):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO fca_firms (
                frn, firm_name, fca_status, is_active, source_url, website_url,
                source_record_hash, first_seen_at, last_seen_at
            ) VALUES ('123456', 'Example', 'Authorised', 1, ?, ?, ?, ?, ?)
            """,
            (
                "https://register.fca.org.uk/s/firm?id=123456",
                website_url,
                "a" * 64,
                "2026-07-25T10:00:00+00:00",
                "2026-07-25T10:00:00+00:00",
            ),
        )


@pytest.mark.parametrize(
    "legacy_website",
    [
        "https://EXAMPLE.com/",
        "https://-bad.example/",
        "https://bad-.example/",
        "https://bad..example/",
        "https://.bad.example/",
        "https://bad.example./",
        "https://bad_host.example/",
        "https://bad~host.example/",
        f"https://{'a' * 64}.example/",
        f"https://{'.'.join(['a' * 63] * 4)}/",
    ],
)
def test_migration_008_refuses_legacy_noncanonical_website_and_rolls_back(
    tmp_path, legacy_website
):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    _migrate_through_007(conn)
    conn.execute(
        """
        INSERT INTO fca_firms (
            frn, firm_name, fca_status, is_active, source_url, website_url,
            source_record_hash, first_seen_at, last_seen_at
        ) VALUES ('123456', 'Example', 'Authorised', 1, ?, ?, ?, ?, ?)
        """,
        (
            "https://register.fca.org.uk/s/firm?id=123456",
            legacy_website,
            "a" * 64,
            "2026-07-25T10:00:00+00:00",
            "2026-07-25T10:00:00+00:00",
        ),
    )

    with pytest.raises(sqlite3.IntegrityError):
        migrate(conn)

    assert conn.execute(
        "SELECT count(*) FROM schema_migrations WHERE version = '008'"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type = 'trigger' AND name = 'fca_website_canonical_insert'"
    ).fetchone()[0] == 0


def test_migration_008_refuses_legacy_linked_company_mismatch_and_rolls_back(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    _migrate_through_007(conn)
    lead_id = _lead(conn, "12345678")
    conn.execute(
        """
        INSERT INTO fca_firms (
            frn, firm_name, fca_status, is_active, source_url, company_number,
            lead_id, source_record_hash, first_seen_at, last_seen_at
        ) VALUES ('123456', 'Example', 'Authorised', 1, ?, '87654321', ?, ?, ?, ?)
        """,
        (
            "https://register.fca.org.uk/s/firm?id=123456",
            lead_id,
            "a" * 64,
            "2026-07-25T10:00:00+00:00",
            "2026-07-25T10:00:00+00:00",
        ),
    )

    with pytest.raises(sqlite3.IntegrityError):
        migrate(conn)

    assert conn.execute(
        "SELECT count(*) FROM schema_migrations WHERE version = '008'"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type = 'trigger' AND name = 'fca_lead_company_number_match_insert'"
    ).fetchone()[0] == 0


def test_linked_fca_identity_cannot_be_mutated_or_rebound_by_direct_sql(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    lead_id = _lead(conn)
    firm_id = conn.execute(
        """
        INSERT INTO fca_firms (
            frn, firm_name, fca_status, is_active, source_url, company_number,
            lead_id, source_record_hash, first_seen_at, last_seen_at
        ) VALUES ('123456', 'Example 12345678 Ltd', 'Authorised', 1, ?,
                  '12345678', ?, ?, ?, ?)
        """,
        (
            "https://register.fca.org.uk/s/firm?id=123456",
            lead_id,
            "a" * 64,
            "2026-07-25T10:00:00+00:00",
            "2026-07-25T10:00:00+00:00",
        ),
    ).lastrowid

    for statement in (
        "UPDATE fca_firms SET firm_name = 'Changed Ltd' WHERE id = ?",
        "UPDATE fca_firms SET is_active = 0 WHERE id = ?",
        "UPDATE fca_firms SET company_number = '87654321' WHERE id = ?",
        "UPDATE fca_firms SET source_record_hash = 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' WHERE id = ?",
        "UPDATE fca_firms SET lead_id = NULL WHERE id = ?",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="linked FCA"):
            conn.execute(statement, (firm_id,))


def test_enrichment_runs_and_retirement_audit_are_immutable_by_direct_sql(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    firm_id = conn.execute(
        """
        INSERT INTO fca_firms (
            frn, firm_name, fca_status, is_active, source_url,
            source_record_hash, first_seen_at, last_seen_at
        ) VALUES ('123456', 'Example', 'Authorised', 1, ?, ?, ?, ?)
        """,
        (
            "https://register.fca.org.uk/s/firm?id=123456",
            "a" * 64,
            "2026-07-25T10:00:00+00:00",
            "2026-07-25T10:00:00+00:00",
        ),
    ).lastrowid
    run_id = conn.execute(
        """
        INSERT INTO enrichment_runs (
            firm_id, state, started_at, completed_at, input_hash, score, temperature
        ) VALUES (?, 'complete', ?, ?, ?, 40, 'COOL')
        """,
        (
            firm_id,
            "2026-07-25T10:00:00+00:00",
            "2026-07-25T10:01:00+00:00",
            "a" * 64,
        ),
    ).lastrowid
    event_id = conn.execute(
        """
        INSERT INTO retirement_events (
            source_register, retired_count, leads_before, sends_before,
            backup_path, backup_sha256, retired_at, note
        ) VALUES ('LCA member directory', 0, 0, 0, '/tmp/backup.sqlite3', ?, ?, 'audit')
        """,
        ("b" * 64, "2026-07-25T10:00:00+00:00"),
    ).lastrowid

    for table, row_id in (("enrichment_runs", run_id), ("retirement_events", event_id)):
        with pytest.raises(sqlite3.IntegrityError, match="immutable|cannot be deleted"):
            conn.execute(f"UPDATE {table} SET id = id WHERE id = ?", (row_id,))
        with pytest.raises(sqlite3.IntegrityError, match="immutable|cannot be deleted"):
            conn.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))


@pytest.mark.parametrize("terminal_state", ["complete", "failed"])
def test_evidence_cannot_be_appended_to_terminal_enrichment_run(tmp_path, terminal_state):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    firm_id = conn.execute(
        """
        INSERT INTO fca_firms (
            frn, firm_name, fca_status, is_active, source_url,
            source_record_hash, first_seen_at, last_seen_at
        ) VALUES ('123456', 'Example', 'Authorised', 1, ?, ?, ?, ?)
        """,
        (
            "https://register.fca.org.uk/s/firm?id=123456",
            "a" * 64,
            "2026-07-25T10:00:00+00:00",
            "2026-07-25T10:00:00+00:00",
        ),
    ).lastrowid
    if terminal_state == "complete":
        run_id = conn.execute(
            """
            INSERT INTO enrichment_runs (
                firm_id, state, started_at, completed_at, input_hash, score, temperature
            ) VALUES (?, 'complete', ?, ?, ?, 40, 'COOL')
            """,
            (
                firm_id,
                "2026-07-25T10:00:00+00:00",
                "2026-07-25T10:01:00+00:00",
                "b" * 64,
            ),
        ).lastrowid
    else:
        run_id = conn.execute(
            """
            INSERT INTO enrichment_runs (
                firm_id, state, started_at, completed_at, input_hash, failure_code
            ) VALUES (?, 'failed', ?, ?, ?, 'FETCH_FAILED')
            """,
            (
                firm_id,
                "2026-07-25T10:00:00+00:00",
                "2026-07-25T10:01:00+00:00",
                "b" * 64,
            ),
        ).lastrowid

    with pytest.raises(sqlite3.IntegrityError, match="terminal enrichment run"):
        conn.execute(
            """
            INSERT INTO evidence_items (
                run_id, signal_group, code, evidence_state, weight,
                source_url, excerpt, observed_at, content_hash
            ) VALUES (?, 'ai_exposure', 'AI_VISIBLE', 'absent', 0,
                      'https://example.test/', NULL, ?, ?)
            """,
            (run_id, "2026-07-25T10:00:00+00:00", "c" * 64),
        )


def test_unverified_directory_candidate_is_staged_without_becoming_a_lead(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)

    candidate_id = conn.execute(
        """
        INSERT INTO candidates (
            source_register, source_url, company_name, source_location,
            source_record_hash, discovered_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "LCA member directory",
            "https://www.legionellacontrolassociation.co.uk/company/example-limited/",
            "Example Limited",
            "London",
            "a" * 64,
            "2026-07-20T14:00:00+00:00",
            "2026-07-20T14:00:00+00:00",
        ),
    ).lastrowid

    candidate = conn.execute(
        "SELECT status FROM candidates WHERE id = ?", (candidate_id,)
    ).fetchone()
    assert candidate[0] == "discovered"
    candidate_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(candidates)")
    }
    assert not {
        "company_number",
        "contact_email",
        "eligible",
        "lead_id",
        "promoted",
    }.intersection(candidate_columns)
    assert conn.execute("SELECT count(*) FROM leads").fetchone()[0] == 0


def test_migration_refuses_checksum_mismatch(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    conn.execute(
        "UPDATE schema_migrations SET checksum = ? WHERE version = '001'",
        ("a" * 64,),
    )

    with pytest.raises(RuntimeError, match="checksum"):
        migrate(conn)


def test_verified_incorporated_company_can_enter_leads(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)

    lead_id = _lead(conn)

    row = conn.execute(
        """
        SELECT company_number, legal_form, company_status,
               verification_source, companies_house_verified_at
        FROM leads WHERE id = ?
        """,
        (lead_id,),
    ).fetchone()
    assert tuple(row[:4]) == (
        "12345678",
        "ltd",
        "active",
        "companies_house_api",
    )
    assert row[4].endswith("+00:00")


def test_verified_company_cannot_be_forged_by_direct_construction():
    with pytest.raises(TypeError, match="CompaniesHouseClient"):
        VerifiedCompany(
            company_number="12345678",
            legal_name="Forged Ltd",
            legal_form="ltd",
            company_status="active",
            verified_at=datetime(2026, 7, 20, 9, 0, tzinfo=UTC),
            verification_source="companies_house_api",
            profile_hash="a" * 64,
            _proof=object(),
        )


def test_verified_company_cannot_be_forged_by_bypassing_init(tmp_path):
    genuine = _verified()
    forged = object.__new__(VerifiedCompany)
    for field_name in (
        "company_number",
        "legal_name",
        "legal_form",
        "company_status",
        "verified_at",
        "verification_source",
        "profile_hash",
    ):
        object.__setattr__(forged, field_name, getattr(genuine, field_name))
    assert forged == genuine
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)

    with pytest.raises(TypeError, match="verified Companies House evidence"):
        insert_verified_lead(
            conn,
            company=forged,
            contact_email="director@example.test",
            source_register="Test register",
        )


def test_lead_contact_email_must_be_one_recipient(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)

    with pytest.raises(ValueError, match="single recipient"):
        insert_verified_lead(
            conn,
            company=_verified(),
            contact_email="first@example.test, second@example.test",
            source_register="Test register",
        )


@pytest.mark.parametrize(
    "contact_email",
    [
        "first@example.test,second@example.test",
        "first@example.test\nBcc:second@example.test",
        "First@Example.test",
    ],
)
def test_database_rejects_noncanonical_lead_recipient(tmp_path, contact_email):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)

    with pytest.raises(
        sqlite3.IntegrityError, match="canonical recipient|control character"
    ):
        conn.execute(
            """
            INSERT INTO leads (
                company_number, legal_name, legal_form, company_status,
                verification_source, companies_house_verified_at,
                companies_house_profile_hash, contact_email,
                source_register, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "12345678",
                "Direct SQL Ltd",
                "ltd",
                "active",
                "companies_house_api",
                "2026-07-20T09:00:00+00:00",
                "a" * 64,
                contact_email,
                "Directory",
                "2026-07-20T09:00:00+00:00",
            ),
        )


@pytest.mark.parametrize("codepoint", [*range(33), 127])
def test_database_rejects_every_ascii_control_in_lead_recipient(
    tmp_path, codepoint
):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)

    with pytest.raises(sqlite3.IntegrityError, match="control character"):
        conn.execute(
            """
            INSERT INTO leads (
                company_number, legal_name, legal_form, company_status,
                verification_source, companies_house_verified_at,
                companies_house_profile_hash, contact_email,
                source_register, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "12345678",
                "Direct SQL Ltd",
                "ltd",
                "active",
                "companies_house_api",
                "2026-07-20T09:00:00+00:00",
                "a" * 64,
                f"director{chr(codepoint)}@example.test",
                "Directory",
                "2026-07-20T09:00:00+00:00",
            ),
        )


@pytest.mark.parametrize(
    "to_email",
    [
        "director-12345678@example.test,second@example.test",
        "director-12345678@example.test\nBcc:second@example.test",
        "other@example.test",
    ],
)
def test_database_rejects_invalid_or_unbound_send_recipient(tmp_path, to_email):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    lead_id = _lead(conn)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO sends (
                lead_id, to_email, stage, template, subject, body_hash,
                word_count, state, created_at
            ) VALUES (?, ?, 0, 'signal-led', 'subject', ?, 10, 'reserved', ?)
            """,
            (
                lead_id,
                to_email,
                "a" * 64,
                "2026-07-20T09:00:00+00:00",
            ),
        )


def test_database_rejects_fabricated_verification_provenance(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO leads (
                company_number, legal_name, legal_form, company_status,
                verification_source, companies_house_verified_at,
                companies_house_profile_hash, contact_email,
                source_register, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "12345678",
                "Bypass Attempt Ltd",
                "ltd",
                "active",
                "directory_claim",
                "2026-07-20T09:00:00+00:00",
                "a" * 64,
                "owner@example.test",
                "Directory",
                "2026-07-20T09:00:00+00:00",
            ),
        )


def test_ledger_rejects_non_hex_hash_and_inconsistent_sent_state(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    lead_id = _lead(conn)
    values = (
        lead_id,
        "director@example.test",
        0,
        "signal-led",
        "your privacy notice and AI",
        "z" * 64,
        10,
        "sent",
        "2026-07-20T09:00:00+00:00",
    )

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO sends (
                lead_id, to_email, stage, template, subject, body_hash,
                word_count, state, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )


@pytest.mark.parametrize(
    ("state", "draft_id", "message_id"),
    [
        ("draft", "", None),
        ("draft", "   ", None),
        ("sent", "draft-1", None),
        ("sent", "draft-1", ""),
        ("sent", "draft-1", "   "),
    ],
)
def test_ledger_requires_concrete_external_identity(
    tmp_path, state, draft_id, message_id
):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    lead_id = _lead(conn)
    sent_at = "2026-07-20T08:02:00+00:00" if state == "sent" else None

    with pytest.raises(sqlite3.IntegrityError, match="concrete Gmail identity"):
        conn.execute(
            """
            INSERT INTO sends (
                lead_id, to_email, stage, template, subject, body_hash,
                word_count, state, created_at, drafted_at, sent_at,
                gmail_draft_id, gmail_message_id
            ) VALUES (?, ?, 0, 'signal-led', 'subject', ?, 10, ?, ?, ?, ?, ?, ?)
            """,
            (
                lead_id,
                "director-12345678@example.test",
                "a" * 64,
                state,
                "2026-07-20T08:00:00+00:00",
                "2026-07-20T08:01:00+00:00",
                sent_at,
                draft_id,
                message_id,
            ),
        )


def test_ledger_rejects_void_state_with_sent_or_failed_timestamps(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    lead_id = _lead(conn)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO sends (
                lead_id, to_email, stage, template, subject, body_hash,
                word_count, state, created_at, drafted_at, sent_at, voided_at,
                gmail_draft_id
            ) VALUES (?, ?, 0, 'signal-led', 'subject', ?, 10, 'void', ?, ?, ?, ?, ?)
            """,
            (
                lead_id,
                "director-12345678@example.test",
                "a" * 64,
                "2026-07-20T08:00:00+00:00",
                "2026-07-20T08:01:00+00:00",
                "2026-07-20T08:02:00+00:00",
                "2026-07-20T08:03:00+00:00",
                "draft-1",
            ),
        )


@pytest.mark.parametrize(
    (
        "state",
        "drafted_at",
        "sent_at",
        "voided_at",
        "failed_at",
        "gmail_draft_id",
        "failure_reason",
    ),
    [
        ("reserved", None, None, None, None, "draft-1", None),
        ("draft", None, None, None, None, "draft-1", None),
        ("draft", "2026-07-20T08:01:00+00:00", None, None, None, None, None),
        (
            "sent",
            "2026-07-20T08:01:00+00:00",
            "2026-07-20T08:02:00+00:00",
            None,
            None,
            None,
            None,
        ),
        ("void", None, None, "2026-07-20T08:03:00+00:00", None, "draft-1", None),
        ("failed", None, None, None, "2026-07-20T08:04:00+00:00", None, None),
        (
            "failed",
            "2026-07-20T08:01:00+00:00",
            None,
            None,
            "2026-07-20T08:04:00+00:00",
            None,
            "failed after drafting",
        ),
    ],
)
def test_ledger_rejects_impossible_state_matrix(
    tmp_path,
    state,
    drafted_at,
    sent_at,
    voided_at,
    failed_at,
    gmail_draft_id,
    failure_reason,
):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    lead_id = _lead(conn)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO sends (
                lead_id, to_email, stage, template, subject, body_hash,
                word_count, state, created_at, drafted_at, sent_at, voided_at,
                failed_at, gmail_draft_id, failure_reason
            ) VALUES (?, ?, 0, 'signal-led', 'subject', ?, 10, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                lead_id,
                "director-12345678@example.test",
                "a" * 64,
                state,
                "2026-07-20T08:00:00+00:00",
                drafted_at,
                sent_at,
                voided_at,
                failed_at,
                gmail_draft_id,
                failure_reason,
            ),
        )


@pytest.mark.parametrize(
    (
        "state",
        "drafted_at",
        "sent_at",
        "voided_at",
        "gmail_draft_id",
        "gmail_message_id",
        "gmail_thread_id",
        "reply_class",
        "replied_at",
    ),
    [
        ("reserved", None, None, None, None, "message-1", None, None, None),
        (
            "reserved",
            None,
            None,
            None,
            None,
            None,
            "thread-1",
            "positive",
            "2026-07-20T08:03:00+00:00",
        ),
        (
            "draft",
            "2026-07-20T07:59:00+00:00",
            None,
            None,
            "draft-1",
            None,
            None,
            None,
            None,
        ),
        (
            "sent",
            "2026-07-20T08:02:00+00:00",
            "2026-07-20T08:01:00+00:00",
            None,
            "draft-1",
            "message-1",
            "thread-1",
            None,
            None,
        ),
        (
            "sent",
            "2026-07-20T08:01:00+00:00",
            "2026-07-20T08:02:00+00:00",
            None,
            "draft-1",
            "message-1",
            "thread-1",
            "positive",
            None,
        ),
        (
            "sent",
            "2026-07-20T08:01:00+00:00",
            "2026-07-20T08:02:00+00:00",
            None,
            "draft-1",
            "message-1",
            "thread-1",
            "positive",
            "2026-07-20T08:01:30+00:00",
        ),
        (
            "void",
            "2026-07-20T08:02:00+00:00",
            None,
            "2026-07-20T08:01:00+00:00",
            "draft-1",
            None,
            None,
            None,
            None,
        ),
    ],
)
def test_ledger_rejects_ancillary_metadata_and_bad_chronology(
    tmp_path,
    state,
    drafted_at,
    sent_at,
    voided_at,
    gmail_draft_id,
    gmail_message_id,
    gmail_thread_id,
    reply_class,
    replied_at,
):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    lead_id = _lead(conn)

    with pytest.raises(sqlite3.IntegrityError, match="invalid send lifecycle"):
        conn.execute(
            """
            INSERT INTO sends (
                lead_id, to_email, stage, template, subject, body_hash,
                word_count, state, created_at, drafted_at, sent_at, voided_at,
                gmail_draft_id, gmail_message_id, gmail_thread_id,
                reply_class, replied_at
            ) VALUES (
                ?, ?, 0, 'signal-led', 'subject', ?, 10, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?
            )
            """,
            (
                lead_id,
                "director-12345678@example.test",
                "a" * 64,
                state,
                "2026-07-20T08:00:00+00:00",
                drafted_at,
                sent_at,
                voided_at,
                gmail_draft_id,
                gmail_message_id,
                gmail_thread_id,
                reply_class,
                replied_at,
            ),
        )


def test_ledger_lifecycle_guard_applies_to_updates(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    lead_id = _lead(conn)
    cursor = conn.execute(
        """
        INSERT INTO sends (
            lead_id, to_email, stage, template, subject, body_hash,
            word_count, state, created_at
        ) VALUES (?, ?, 0, 'signal-led', 'subject', ?, 10, 'reserved', ?)
        """,
        (
            lead_id,
            "director-12345678@example.test",
            "a" * 64,
            "2026-07-20T08:00:00+00:00",
        ),
    )

    with pytest.raises(sqlite3.IntegrityError, match="invalid send lifecycle"):
        conn.execute(
            "UPDATE sends SET gmail_message_id = 'message-1' WHERE id = ?",
            (cursor.lastrowid,),
        )


@pytest.mark.parametrize(
    ("old_state", "new_state"),
    [("sent", "draft"), ("void", "draft"), ("failed", "reserved")],
)
def test_terminal_ledger_states_cannot_move_backwards(tmp_path, old_state, new_state):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    send_id = _insert_send_in_state(conn, _lead(conn), old_state)

    with pytest.raises(sqlite3.IntegrityError, match="invalid send state transition"):
        conn.execute(
            "UPDATE sends SET state = ? WHERE id = ?",
            (new_state, send_id),
        )


def test_send_rows_cannot_be_deleted_or_rewritten(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    send_id = _insert_send_in_state(conn, _lead(conn), "sent")

    with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
        conn.execute("DELETE FROM sends WHERE id = ?", (send_id,))
    with pytest.raises(sqlite3.IntegrityError, match="audit identity is immutable"):
        conn.execute("UPDATE sends SET subject = 'rewritten' WHERE id = ?", (send_id,))
    with pytest.raises(sqlite3.IntegrityError, match="terminal send evidence"):
        conn.execute(
            "UPDATE sends SET gmail_message_id = 'replacement' WHERE id = ?",
            (send_id,),
        )


def test_reply_classification_is_append_once(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    send_id = _insert_send_in_state(conn, _lead(conn), "sent")
    conn.execute(
        """
        UPDATE sends
        SET reply_class = 'positive', replied_at = '2026-07-20T08:03:00+00:00'
        WHERE id = ?
        """,
        (send_id,),
    )

    with pytest.raises(sqlite3.IntegrityError, match="classification is immutable"):
        conn.execute(
            """
            UPDATE sends
            SET reply_class = 'negative', replied_at = '2026-07-20T08:04:00+00:00'
            WHERE id = ?
            """,
            (send_id,),
        )


def test_lead_evidence_is_immutable_after_send_history_exists(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    lead_id = _lead(conn)
    _insert_send_in_state(conn, lead_id, "reserved")

    with pytest.raises(sqlite3.IntegrityError, match="lead evidence is immutable"):
        conn.execute(
            "UPDATE leads SET contact_email = 'other@example.test' WHERE id = ?",
            (lead_id,),
        )


def test_database_file_is_owner_only(tmp_path):
    database = tmp_path / "private" / "govscout.sqlite3"
    conn = connect_database(database)
    migrate(conn)
    conn.close()

    assert os.stat(database).st_mode & 0o777 == 0o600
    assert os.stat(database.parent).st_mode & 0o777 == 0o700


def test_allowed_legal_forms_are_incorporated_only():
    assert ALLOWED_LEGAL_FORMS == frozenset(
        {"ltd", "plc", "llp", "cic", "charitable_company"}
    )
