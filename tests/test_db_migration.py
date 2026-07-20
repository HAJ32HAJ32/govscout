from datetime import UTC, datetime
import os
import sqlite3

import pytest

from govscout.companies_house import VerifiedCompany
from govscout.db import (
    ALLOWED_LEGAL_FORMS,
    connect_database,
    insert_verified_lead,
    migrate,
)
from tests.support import (
    verified_company_from_test_profile as verified_company_from_profile,
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
    assert {"schema_migrations", "app_state", "leads", "sends"}.issubset(tables)
    migrations = conn.execute(
        "SELECT version, length(checksum) FROM schema_migrations ORDER BY version"
    ).fetchall()
    assert [tuple(row) for row in migrations] == [
        ("001", 64),
        ("002", 64),
        ("003", 64),
        ("004", 64),
    ]


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
