from datetime import UTC, datetime
import sqlite3

from govscout.db import connect_database, migrate
import pytest

import govscout.retirement as retirement
from govscout.retirement import create_verified_backup, retire_lca_candidates


def test_lca_retirement_deletes_only_legacy_candidates_and_records_sanitised_audit(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    conn.execute(
        """
        INSERT INTO candidates (
            source_register, source_url, company_name, source_location,
            source_record_hash, discovered_at, last_seen_at
        ) VALUES ('LCA member directory', ?, 'Historical Candidate Ltd', 'London', ?, ?, ?)
        """,
        (
            "https://www.legionellacontrolassociation.co.uk/company/historical/",
            "a" * 64,
            "2026-07-20T10:00:00+00:00",
            "2026-07-20T10:00:00+00:00",
        ),
    )

    receipt = create_verified_backup(conn, tmp_path / "before-retirement.sqlite3")
    result = retire_lca_candidates(
        conn, backup_receipt=receipt, now=datetime(2026, 7, 25, 14, tzinfo=UTC)
    )

    assert result.retired_count == 1
    assert result.leads_count == 0
    assert result.sends_count == 0
    assert conn.execute("SELECT count(*) FROM candidates").fetchone()[0] == 0
    audit = conn.execute(
        "SELECT source_register, retired_count, note FROM retirement_events"
    ).fetchone()
    assert tuple(audit) == (
        "LCA member directory",
        1,
        "Legacy LCA candidate staging retired; no raw candidate data retained",
    )


def test_lca_retirement_rejects_stale_backup_receipt_without_deleting(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    receipt = create_verified_backup(conn, tmp_path / "before-retirement.sqlite3")
    conn.execute(
        """
        INSERT INTO candidates (
            source_register, source_url, company_name, source_record_hash,
            discovered_at, last_seen_at
        ) VALUES ('LCA member directory', ?, 'Late Candidate Ltd', ?, ?, ?)
        """,
        (
            "https://www.legionellacontrolassociation.co.uk/company/late/",
            "b" * 64,
            "2026-07-25T13:00:00+00:00",
            "2026-07-25T13:00:00+00:00",
        ),
    )

    with pytest.raises(ValueError, match="no longer matches"):
        retire_lca_candidates(
            conn,
            backup_receipt=receipt,
            now=datetime(2026, 7, 25, 14, tzinfo=UTC),
        )

    assert conn.execute("SELECT count(*) FROM candidates").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM retirement_events").fetchone()[0] == 0


def test_lca_retirement_rejects_backup_modified_after_verification(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    backup_path = tmp_path / "before-retirement.sqlite3"
    receipt = create_verified_backup(conn, backup_path)
    backup_path.write_bytes(b"not sqlite")

    with pytest.raises(ValueError, match="backup receipt"):
        retire_lca_candidates(
            conn,
            backup_receipt=receipt,
            now=datetime(2026, 7, 25, 14, tzinfo=UTC),
        )


def test_lca_retirement_revalidates_backup_inside_write_transaction(tmp_path, monkeypatch):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    conn.execute(
        """
        INSERT INTO candidates (
            source_register, source_url, company_name, source_record_hash,
            discovered_at, last_seen_at
        ) VALUES ('LCA member directory', ?, 'Historical Candidate Ltd', ?, ?, ?)
        """,
        (
            "https://www.legionellacontrolassociation.co.uk/company/historical/",
            "a" * 64,
            "2026-07-20T10:00:00+00:00",
            "2026-07-20T10:00:00+00:00",
        ),
    )
    backup_path = tmp_path / "before-retirement.sqlite3"
    receipt = create_verified_backup(conn, backup_path)
    original_verify = retirement._verify_backup_receipt
    verification_count = 0

    def corrupt_after_initial_verification(connection, backup_receipt):
        nonlocal verification_count
        verification_count += 1
        original_verify(connection, backup_receipt)
        if verification_count == 1:
            backup_path.write_bytes(b"corrupted after initial verification")

    monkeypatch.setattr(retirement, "_verify_backup_receipt", corrupt_after_initial_verification)

    with pytest.raises(ValueError, match="backup receipt hash"):
        retire_lca_candidates(
            conn,
            backup_receipt=receipt,
            now=datetime(2026, 7, 25, 14, tzinfo=UTC),
        )

    assert verification_count == 2
    assert conn.execute("SELECT count(*) FROM candidates").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM retirement_events").fetchone()[0] == 0


def test_lca_retirement_rolls_back_when_post_delete_integrity_gate_fails(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    conn.execute(
        """
        INSERT INTO candidates (
            source_register, source_url, company_name, source_record_hash,
            discovered_at, last_seen_at
        ) VALUES ('LCA member directory', ?, 'Historical Candidate Ltd', ?, ?, ?)
        """,
        (
            "https://www.legionellacontrolassociation.co.uk/company/historical/",
            "a" * 64,
            "2026-07-20T10:00:00+00:00",
            "2026-07-20T10:00:00+00:00",
        ),
    )
    receipt = create_verified_backup(conn, tmp_path / "before-retirement.sqlite3")
    delete_seen = False
    post_delete_pragmas = []

    def deny_post_delete_integrity(action, arg1, _arg2, _database, _trigger):
        nonlocal delete_seen
        if action == sqlite3.SQLITE_DELETE and arg1 == "candidates":
            delete_seen = True
        if action == sqlite3.SQLITE_PRAGMA and delete_seen:
            post_delete_pragmas.append(arg1)
            if arg1 == "integrity_check":
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(deny_post_delete_integrity)

    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        retire_lca_candidates(
            conn,
            backup_receipt=receipt,
            now=datetime(2026, 7, 25, 14, tzinfo=UTC),
        )

    conn.set_authorizer(None)
    assert post_delete_pragmas[:2] == ["foreign_key_check", "integrity_check"]
    assert conn.execute("SELECT count(*) FROM candidates").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM retirement_events").fetchone()[0] == 0
