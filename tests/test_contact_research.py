from datetime import timedelta
import sqlite3

import pytest

from govscout.contact_research import record_contact_evidence
from govscout.db import connect_database, migrate
from govscout.fca_pipeline import verify_firm
from govscout.website_research import WebsiteResearchConflict
from tests.test_processing_queue import NOW, _companies_house, _queue_firm


def _verified_firm(conn):
    _queue_firm(conn, website=None)
    firm = conn.execute("SELECT * FROM fca_firms WHERE frn = '123456'").fetchone()
    verification = verify_firm(
        conn,
        firm_id=firm["id"],
        companies_house=_companies_house(),
        now=NOW,
    )
    assert verification.verified is True
    return firm


def test_assertion_and_withdrawal_are_append_only_and_stale_fenced(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    firm = _verified_firm(conn)

    asserted = record_contact_evidence(
        conn,
        firm_id=firm["id"],
        action="assert",
        email="compliance@example.test",
        phone="+44 20 7946 0000",
        contact_name="Jane Compliance",
        evidence_url="https://register.fca.org.uk/s/firm?id=abc123",
        justification="Listed as the compliance contact on the FCA firm page.",
        actor="local-operator",
        expected_previous_event_id=None,
        now=NOW,
    )
    with pytest.raises(WebsiteResearchConflict):
        record_contact_evidence(
            conn,
            firm_id=firm["id"],
            action="assert",
            email="stale@example.test",
            phone=None,
            contact_name=None,
            evidence_url="https://register.fca.org.uk/s/firm?id=abc123",
            justification="Stale submission based on an old page load.",
            actor="local-operator",
            expected_previous_event_id=None,
            now=NOW,
        )
    withdrawn = record_contact_evidence(
        conn,
        firm_id=firm["id"],
        action="withdraw",
        email=None,
        phone=None,
        contact_name=None,
        evidence_url="https://register.fca.org.uk/s/firm?id=abc123",
        justification="Contact left the firm; no longer a valid point of contact.",
        actor="local-operator",
        expected_previous_event_id=asserted,
        now=NOW + timedelta(minutes=1),
    )

    assert [
        tuple(row)
        for row in conn.execute(
            "SELECT id, action, expected_previous_event_id, email, phone, contact_name "
            "FROM firm_contact_evidence_events ORDER BY id"
        )
    ] == [
        (asserted, "assert", None, "compliance@example.test", "+44 20 7946 0000", "Jane Compliance"),
        (withdrawn, "withdraw", asserted, None, None, None),
    ]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE firm_contact_evidence_events SET action = 'assert' WHERE id = ?",
            (withdrawn,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM firm_contact_evidence_events WHERE id = ?", (asserted,))


def test_assertion_requires_at_least_one_contact_field(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    firm = _verified_firm(conn)

    with pytest.raises(ValueError, match="at least one"):
        record_contact_evidence(
            conn,
            firm_id=firm["id"],
            action="assert",
            email=None,
            phone=None,
            contact_name=None,
            evidence_url="https://register.fca.org.uk/s/firm?id=abc123",
            justification="Nothing was actually found on the page.",
            actor="local-operator",
            expected_previous_event_id=None,
            now=NOW,
        )


def test_invalid_email_is_rejected(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    firm = _verified_firm(conn)

    with pytest.raises(ValueError, match="email"):
        record_contact_evidence(
            conn,
            firm_id=firm["id"],
            action="assert",
            email="not-an-email",
            phone=None,
            contact_name=None,
            evidence_url="https://register.fca.org.uk/s/firm?id=abc123",
            justification="Testing an invalid email address value.",
            actor="local-operator",
            expected_previous_event_id=None,
            now=NOW,
        )


def test_archived_firm_cannot_receive_contact_evidence(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    firm = _verified_firm(conn)
    conn.execute(
        """
        INSERT INTO firm_archive_events (
            firm_id, action, reason, actor, expected_previous_event_id, occurred_at
        ) VALUES (?, 'archive', 'Outside current target market', 'test-operator', NULL, ?)
        """,
        (firm["id"], NOW.isoformat()),
    )

    with pytest.raises(sqlite3.IntegrityError, match="archived firm"):
        record_contact_evidence(
            conn,
            firm_id=firm["id"],
            action="assert",
            email="compliance@example.test",
            phone=None,
            contact_name=None,
            evidence_url="https://register.fca.org.uk/s/firm?id=abc123",
            justification="Listed as the compliance contact on the FCA firm page.",
            actor="local-operator",
            expected_previous_event_id=None,
            now=NOW,
        )


def test_database_rejects_mismatched_fca_identity(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    firm = _verified_firm(conn)
    import_id = conn.execute(
        "SELECT import_id FROM collector_imports LIMIT 1"
    ).fetchone()[0]

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO firm_contact_evidence_events (
                firm_id, action, email, evidence_url, justification,
                actor, occurred_at, expected_previous_event_id,
                fca_source_record_hash, collector_import_id
            ) VALUES (?, 'assert', 'compliance@example.test',
                      'https://register.fca.org.uk/s/firm?id=abc123', 'Plausible but mismatched',
                      'local-operator', ?, NULL, ?, ?)
            """,
            (firm["id"], NOW.isoformat(), "f" * 64, import_id),
        )
