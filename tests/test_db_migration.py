from datetime import UTC, datetime
import os
import sqlite3

import pytest

from govscout.companies_house import VerifiedCompany, verified_company_from_profile
from govscout.db import (
    ALLOWED_LEGAL_FORMS,
    connect_database,
    insert_verified_lead,
    migrate,
)


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


def test_migration_is_versioned_idempotent_and_creates_p1_tables(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")

    migrate(conn)
    migrate(conn)

    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {"schema_migrations", "app_state", "leads", "sends"}.issubset(tables)
    migration = conn.execute(
        "SELECT version, length(checksum) FROM schema_migrations"
    ).fetchone()
    assert tuple(migration) == ("001", 64)


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
    with pytest.raises(TypeError, match="verification receipt"):
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
