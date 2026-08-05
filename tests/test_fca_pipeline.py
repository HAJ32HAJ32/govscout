from datetime import UTC, datetime
import sqlite3

import pytest

import govscout.fca_pipeline as fca_pipeline_module
from govscout.companies_house import CompaniesHouseClient
from govscout.companies_house_http import CompaniesHouseTransportError
from govscout.db import connect_database, insert_verified_lead, migrate
from govscout.fca_discovery import ingest_fca_records, parse_fca_json
from govscout.fca_pipeline import FcaEligibilityError, verify_and_promote_firm, verify_firm
from tests.support import StubCompaniesHouseTransport


def _stage(conn, *, status="Authorised", name="Example Finance Ltd", number="12345678"):
    payload = (
        "{\"firms\":[{"
        '"frn":"123456",'
        f'"firm_name":"{name}",'
        f'"status":"{status}",'
        '"firm_type":"Regulated firm",'
        '"source_url":"https://register.fca.org.uk/s/firm?id=123456",'
        '"website_url":"https://example.test/",'
        '"location":"London",'
        f'"company_number":"{number}"'
        "}]}"
    ).encode()
    ingest_fca_records(
        conn,
        parse_fca_json(payload),
        limit=1,
        now=datetime(2026, 7, 25, 10, tzinfo=UTC),
    )
    return conn.execute("SELECT id FROM fca_firms").fetchone()[0]


def test_active_fca_firm_promotes_only_through_companies_house_receipt(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    firm_id = _stage(conn)
    transport = StubCompaniesHouseTransport(
        {
            "company_number": "12345678",
            "company_name": "Example Finance Ltd",
            "company_status": "active",
            "type": "ltd",
        }
    )

    lead_id = verify_and_promote_firm(
        conn,
        firm_id=firm_id,
        companies_house=CompaniesHouseClient(transport),
        contact_email="compliance@example.test",
        now=datetime(2026, 7, 25, 11, tzinfo=UTC),
    )
    retry_id = verify_and_promote_firm(
        conn,
        firm_id=firm_id,
        companies_house=CompaniesHouseClient(transport),
        contact_email="compliance@example.test",
        now=datetime(2026, 7, 25, 11, tzinfo=UTC),
    )

    assert retry_id == lead_id
    assert transport.requests == ["12345678"]
    row = conn.execute(
        "SELECT source_register, company_status FROM leads WHERE id = ?", (lead_id,)
    ).fetchone()
    assert tuple(row) == ("FCA Financial Services Register", "active")
    assert conn.execute("SELECT lead_id FROM fca_firms").fetchone()[0] == lead_id
    assert conn.execute(
        "SELECT count(*) FROM company_verification_attempts WHERE state = 'verified'"
    ).fetchone()[0] == 1


def test_fca_firm_can_be_verified_without_inventing_a_contact(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    firm_id = _stage(conn)
    transport = StubCompaniesHouseTransport(
        {
            "company_number": "12345678",
            "company_name": "Example Finance Ltd",
            "company_status": "active",
            "type": "ltd",
        }
    )
    now = datetime(2026, 8, 5, 9, tzinfo=UTC)

    result = verify_firm(
        conn,
        firm_id=firm_id,
        companies_house=CompaniesHouseClient(transport),
        now=now,
    )
    retry = verify_firm(
        conn,
        firm_id=firm_id,
        companies_house=CompaniesHouseClient(transport),
        now=now,
    )

    assert result.verified is True
    assert retry.attempt_id == result.attempt_id
    assert transport.requests == ["12345678"]
    assert conn.execute("SELECT count(*) FROM leads").fetchone()[0] == 0
    row = conn.execute(
        "SELECT state, reason_code, legal_name FROM company_verification_attempts"
    ).fetchone()
    assert tuple(row) == ("verified", "VERIFIED", "Example Finance Ltd")


def test_fca_reverification_appends_a_fresh_immutable_receipt(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    firm_id = _stage(conn)
    transport = StubCompaniesHouseTransport(
        {
            "company_number": "12345678",
            "company_name": "Example Finance Ltd",
            "company_status": "active",
            "type": "ltd",
        }
    )

    first = verify_firm(
        conn,
        firm_id=firm_id,
        companies_house=CompaniesHouseClient(transport),
        now=datetime(2026, 8, 5, 9, tzinfo=UTC),
    )
    refreshed = verify_firm(
        conn,
        firm_id=firm_id,
        companies_house=CompaniesHouseClient(transport),
        now=datetime(2026, 8, 6, 9, tzinfo=UTC),
        force_refresh=True,
    )

    assert refreshed.attempt_id != first.attempt_id
    assert transport.requests == ["12345678", "12345678"]
    assert conn.execute("SELECT count(*) FROM company_verification_attempts").fetchone()[0] == 2


def test_ordinary_verification_does_not_reuse_success_before_newer_failure(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    firm_id = _stage(conn)
    delegate = CompaniesHouseClient(
        StubCompaniesHouseTransport(
            {
                "company_number": "12345678",
                "company_name": "Example Finance Ltd",
                "company_status": "active",
                "type": "ltd",
            }
        )
    )

    class FailSecondVerification:
        calls = 0

        def verify_company(self, company_number, *, now):
            self.calls += 1
            if self.calls == 2:
                raise CompaniesHouseTransportError("TEMPORARILY_UNAVAILABLE")
            return delegate.verify_company(company_number, now=now)

    verifier = FailSecondVerification()
    first = verify_firm(
        conn,
        firm_id=firm_id,
        companies_house=verifier,
        now=datetime(2026, 8, 5, 9, tzinfo=UTC),
    )
    with pytest.raises(CompaniesHouseTransportError):
        verify_firm(
            conn,
            firm_id=firm_id,
            companies_house=verifier,
            now=datetime(2026, 8, 6, 9, tzinfo=UTC),
            force_refresh=True,
        )

    recovered = verify_firm(
        conn,
        firm_id=firm_id,
        companies_house=verifier,
        now=datetime(2026, 8, 6, 10, tzinfo=UTC),
    )

    assert verifier.calls == 3
    assert recovered.reused is False
    assert recovered.attempt_id != first.attempt_id
    assert [
        tuple(row)
        for row in conn.execute(
            "SELECT state, reason_code FROM company_verification_attempts ORDER BY id"
        )
    ] == [
        ("verified", "VERIFIED"),
        ("error", "TEMPORARILY_UNAVAILABLE"),
        ("verified", "VERIFIED"),
    ]


def test_fca_verification_records_name_mismatch_without_creating_a_lead(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    firm_id = _stage(conn)

    with pytest.raises(FcaEligibilityError, match="name does not match"):
        verify_firm(
            conn,
            firm_id=firm_id,
            companies_house=CompaniesHouseClient(
                StubCompaniesHouseTransport(
                    {
                        "company_number": "12345678",
                        "company_name": "Different Finance Ltd",
                        "company_status": "active",
                        "type": "ltd",
                    }
                )
            ),
            now=datetime(2026, 8, 5, 9, tzinfo=UTC),
        )

    row = conn.execute(
        "SELECT state, reason_code FROM company_verification_attempts"
    ).fetchone()
    assert tuple(row) == ("ineligible", "LEGAL_NAME_MISMATCH")


def test_fca_verification_records_sanitised_transport_error(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    firm_id = _stage(conn)

    class UnsafeErrorVerifier:
        def verify_company(self, company_number, *, now):
            raise CompaniesHouseTransportError("key=must-not-be-recorded")

    with pytest.raises(CompaniesHouseTransportError, match="must-not-be-recorded"):
        verify_firm(
            conn,
            firm_id=firm_id,
            companies_house=UnsafeErrorVerifier(),
            now=datetime(2026, 8, 5, 9, tzinfo=UTC),
        )

    assert tuple(
        conn.execute(
            "SELECT state, reason_code FROM company_verification_attempts"
        ).fetchone()
    ) == ("error", "TRANSPORT_ERROR")


def test_contact_promotion_rejects_ambiguous_company_number(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    firm_id = _stage(conn)
    conn.execute(
        """
        INSERT INTO fca_firms (
            frn, firm_name, fca_status, firm_type, is_active, source_url,
            website_url, source_location, company_number, source_record_hash,
            first_seen_at, last_seen_at
        ) VALUES ('234567', 'Second FCA Identity Ltd', 'Authorised', 'Regulated firm',
                  1, 'https://register.fca.org.uk/s/firm?id=234567',
                  'https://second.example.test/', 'London', '12345678', ?, ?, ?)
        """,
        (
            "d" * 64,
            "2026-08-05T09:00:00+00:00",
            "2026-08-05T09:00:00+00:00",
        ),
    )
    transport = StubCompaniesHouseTransport(
        {
            "company_number": "12345678",
            "company_name": "Example Finance Ltd",
            "company_status": "active",
            "type": "ltd",
        }
    )

    with pytest.raises(FcaEligibilityError, match="AMBIGUOUS_COMPANY_NUMBER"):
        verify_and_promote_firm(
            conn,
            firm_id=firm_id,
            companies_house=CompaniesHouseClient(transport),
            contact_email="compliance@example.test",
            now=datetime(2026, 8, 5, 9, tzinfo=UTC),
        )

    assert transport.requests == []
    assert conn.execute("SELECT count(*) FROM leads").fetchone()[0] == 0


def test_contact_promotion_records_failed_legal_verification(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    firm_id = _stage(conn)

    with pytest.raises(FcaEligibilityError, match="name does not match"):
        verify_and_promote_firm(
            conn,
            firm_id=firm_id,
            companies_house=CompaniesHouseClient(
                StubCompaniesHouseTransport(
                    {
                        "company_number": "12345678",
                        "company_name": "Different Finance Ltd",
                        "company_status": "active",
                        "type": "ltd",
                    }
                )
            ),
            contact_email="compliance@example.test",
            now=datetime(2026, 8, 5, 9, tzinfo=UTC),
        )

    assert tuple(
        conn.execute(
            "SELECT state, reason_code FROM company_verification_attempts"
        ).fetchone()
    ) == ("ineligible", "LEGAL_NAME_MISMATCH")
    assert conn.execute("SELECT count(*) FROM leads").fetchone()[0] == 0


def test_contact_promotion_rechecks_company_number_ambiguity_after_verification(
    tmp_path,
):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    firm_id = _stage(conn)
    delegate = CompaniesHouseClient(
        StubCompaniesHouseTransport(
            {
                "company_number": "12345678",
                "company_name": "Example Finance Ltd",
                "company_status": "active",
                "type": "ltd",
            }
        )
    )
    concurrent_conn = connect_database(tmp_path / "govscout.sqlite3")

    class DuplicatingVerifier:
        def verify_company(self, company_number, *, now):
            company = delegate.verify_company(company_number, now=now)
            concurrent_conn.execute(
                """
                INSERT INTO fca_firms (
                    frn, firm_name, fca_status, firm_type, is_active, source_url,
                    website_url, source_location, company_number, source_record_hash,
                    first_seen_at, last_seen_at
                ) VALUES ('234567', 'Second FCA Identity Ltd', 'Authorised',
                          'Regulated firm', 1,
                          'https://register.fca.org.uk/s/firm?id=234567',
                          'https://second.example.test/', 'London', '12345678',
                          ?, ?, ?)
                """,
                ("d" * 64, now.isoformat(), now.isoformat()),
            )
            return company

    with pytest.raises(FcaEligibilityError, match="AMBIGUOUS_COMPANY_NUMBER"):
        verify_and_promote_firm(
            conn,
            firm_id=firm_id,
            companies_house=DuplicatingVerifier(),
            contact_email="compliance@example.test",
            now=datetime(2026, 8, 5, 9, tzinfo=UTC),
        )

    concurrent_conn.close()
    assert conn.execute("SELECT count(*) FROM leads").fetchone()[0] == 0


def test_contact_promotion_rejects_newer_failed_attempt_after_verification(
    tmp_path, monkeypatch
):
    database = tmp_path / "govscout.sqlite3"
    conn = connect_database(database)
    migrate(conn)
    firm_id = _stage(conn)
    concurrent_conn = connect_database(database)
    original_verify_firm = fca_pipeline_module.verify_firm

    def verify_then_fail(*args, **kwargs):
        result = original_verify_firm(*args, **kwargs)
        concurrent_conn.execute(
            """
            INSERT INTO company_verification_attempts (
                firm_id, company_number, state, reason_code, checked_at,
                fca_source_record_hash
            ) VALUES (?, '12345678', 'error', 'TEMPORARILY_UNAVAILABLE', ?, ?)
            """,
            (firm_id, kwargs["now"].isoformat(), "a" * 64),
        )
        return result

    monkeypatch.setattr(fca_pipeline_module, "verify_firm", verify_then_fail)
    companies_house = CompaniesHouseClient(
        StubCompaniesHouseTransport(
            {
                "company_number": "12345678",
                "company_name": "Example Finance Ltd",
                "company_status": "active",
                "type": "ltd",
            }
        )
    )

    with pytest.raises(FcaEligibilityError, match="verification changed during promotion"):
        verify_and_promote_firm(
            conn,
            firm_id=firm_id,
            companies_house=companies_house,
            contact_email="compliance@example.test",
            now=datetime(2026, 8, 5, 9, tzinfo=UTC),
        )

    concurrent_conn.close()
    assert conn.execute("SELECT count(*) FROM leads").fetchone()[0] == 0
    assert conn.execute(
        "SELECT lead_id FROM fca_firms WHERE id = ?", (firm_id,)
    ).fetchone()[0] is None
    assert conn.execute(
        """
        SELECT state FROM company_verification_attempts
        WHERE firm_id = ? ORDER BY id DESC LIMIT 1
        """,
        (firm_id,),
    ).fetchone()[0] == "error"


@pytest.mark.parametrize(
    ("stage_changes", "profile_changes", "message"),
    [
        ({"status": "Cancelled"}, {}, "not active"),
        ({"number": ""}, {}, "company number"),
        ({}, {"company_name": "Different Finance Ltd"}, "name does not match"),
        ({}, {"company_status": "dissolved"}, "not active"),
    ],
)
def test_fca_promotion_fails_closed_without_matching_active_incorporated_evidence(
    tmp_path, stage_changes, profile_changes, message
):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    if stage_changes.get("number") == "":
        stage_changes["number"] = None
        payload = (
            b'{"firms":[{"frn":"123456","firm_name":"Example Finance Ltd",'
            b'"status":"Authorised","firm_type":"Regulated firm",'
            b'"source_url":"https://register.fca.org.uk/s/firm?id=123456",'
            b'"website_url":"https://example.test/","location":"London",'
            b'"company_number":null}]}'
        )
        ingest_fca_records(
            conn,
            parse_fca_json(payload),
            limit=1,
            now=datetime(2026, 7, 25, 10, tzinfo=UTC),
        )
        firm_id = conn.execute("SELECT id FROM fca_firms").fetchone()[0]
    else:
        firm_id = _stage(conn, **stage_changes)
    profile = {
        "company_number": "12345678",
        "company_name": "Example Finance Ltd",
        "company_status": "active",
        "type": "ltd",
    }
    profile.update(profile_changes)

    with pytest.raises((FcaEligibilityError, ValueError), match=message):
        verify_and_promote_firm(
            conn,
            firm_id=firm_id,
            companies_house=CompaniesHouseClient(StubCompaniesHouseTransport(profile)),
            contact_email="compliance@example.test",
            now=datetime(2026, 7, 25, 11, tzinfo=UTC),
        )

    assert conn.execute("SELECT count(*) FROM leads").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("firm_name", "Changed Finance Ltd"),
        ("is_active", 0),
        ("company_number", "87654321"),
        ("source_record_hash", "f" * 64),
    ],
)
def test_fca_promotion_rejects_eligibility_snapshot_changed_during_verification(
    tmp_path, column, value
):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    firm_id = _stage(conn)
    delegate = CompaniesHouseClient(
        StubCompaniesHouseTransport(
            {
                "company_number": "12345678",
                "company_name": "Example Finance Ltd",
                "company_status": "active",
                "type": "ltd",
            }
        )
    )

    class MutatingVerifier:
        def verify_company(self, company_number, *, now):
            company = delegate.verify_company(company_number, now=now)
            conn.execute(f"UPDATE fca_firms SET {column} = ? WHERE id = ?", (value, firm_id))
            return company

    with pytest.raises(FcaEligibilityError, match="changed during verification"):
        verify_and_promote_firm(
            conn,
            firm_id=firm_id,
            companies_house=MutatingVerifier(),
            contact_email="compliance@example.test",
            now=datetime(2026, 7, 25, 11, tzinfo=UTC),
        )

    assert conn.execute("SELECT count(*) FROM leads").fetchone()[0] == 0


def test_fca_promotion_rejects_lead_link_created_during_verification(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    firm_id = _stage(conn)
    delegate = CompaniesHouseClient(
        StubCompaniesHouseTransport(
            {
                "company_number": "12345678",
                "company_name": "Example Finance Ltd",
                "company_status": "active",
                "type": "ltd",
            }
        )
    )
    other_company = CompaniesHouseClient(
        StubCompaniesHouseTransport(
            {
                "company_number": "87654321",
                "company_name": "Other Finance Ltd",
                "company_status": "active",
                "type": "ltd",
            }
        )
    ).verify_company("87654321", now=datetime(2026, 7, 25, 10, tzinfo=UTC))
    other_lead = insert_verified_lead(
        conn,
        company=other_company,
        contact_email="other@example.test",
        source_register="Test register",
    )

    class LinkingVerifier:
        def verify_company(self, company_number, *, now):
            company = delegate.verify_company(company_number, now=now)
            conn.execute(
                "UPDATE fca_firms SET lead_id = ? WHERE id = ?", (other_lead, firm_id)
            )
            return company

    with pytest.raises(
        (FcaEligibilityError, sqlite3.IntegrityError),
        match="changed during verification|company numbers must match",
    ):
        verify_and_promote_firm(
            conn,
            firm_id=firm_id,
            companies_house=LinkingVerifier(),
            contact_email="compliance@example.test",
            now=datetime(2026, 7, 25, 11, tzinfo=UTC),
        )


def test_fca_promotion_binds_frn_and_source_url_across_company_verification(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    firm_id = _stage(conn)
    delegate = CompaniesHouseClient(
        StubCompaniesHouseTransport(
            {
                "company_number": "12345678",
                "company_name": "Example Finance Ltd",
                "company_status": "active",
                "type": "ltd",
            }
        )
    )

    class IdentitySwappingVerifier:
        def verify_company(self, company_number, *, now):
            company = delegate.verify_company(company_number, now=now)
            conn.execute(
                """
                UPDATE fca_firms
                SET frn = '654321',
                    source_url = 'https://register.fca.org.uk/s/firm?id=654321'
                WHERE id = ?
                """,
                (firm_id,),
            )
            return company

    with pytest.raises(FcaEligibilityError, match="changed during verification"):
        verify_and_promote_firm(
            conn,
            firm_id=firm_id,
            companies_house=IdentitySwappingVerifier(),
            contact_email="compliance@example.test",
            now=datetime(2026, 7, 25, 11, tzinfo=UTC),
        )

    assert conn.execute("SELECT count(*) FROM leads").fetchone()[0] == 0


def test_fca_promotion_rejects_verified_company_number_mismatching_snapshot(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    firm_id = _stage(conn)
    other_company = CompaniesHouseClient(
        StubCompaniesHouseTransport(
            {
                "company_number": "87654321",
                "company_name": "Example Finance Ltd",
                "company_status": "active",
                "type": "ltd",
            }
        )
    ).verify_company("87654321", now=datetime(2026, 7, 25, 11, tzinfo=UTC))

    class WrongCompanyVerifier:
        def verify_company(self, company_number, *, now):
            return other_company

    with pytest.raises(FcaEligibilityError, match="company number does not match"):
        verify_and_promote_firm(
            conn,
            firm_id=firm_id,
            companies_house=WrongCompanyVerifier(),
            contact_email="compliance@example.test",
            now=datetime(2026, 7, 25, 11, tzinfo=UTC),
        )

    assert conn.execute("SELECT count(*) FROM leads").fetchone()[0] == 0


def test_database_rejects_mismatched_fca_lead_identity_and_later_lead_rekey(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    firm_id = _stage(conn)
    other_company = CompaniesHouseClient(
        StubCompaniesHouseTransport(
            {
                "company_number": "87654321",
                "company_name": "Other Finance Ltd",
                "company_status": "active",
                "type": "ltd",
            }
        )
    ).verify_company("87654321", now=datetime(2026, 7, 25, 11, tzinfo=UTC))
    other_lead = insert_verified_lead(
        conn,
        company=other_company,
        contact_email="other@example.test",
        source_register="Test register",
    )

    with pytest.raises(sqlite3.IntegrityError, match="company number"):
        conn.execute("UPDATE fca_firms SET lead_id = ? WHERE id = ?", (other_lead, firm_id))

    matching_company = CompaniesHouseClient(
        StubCompaniesHouseTransport(
            {
                "company_number": "12345678",
                "company_name": "Example Finance Ltd",
                "company_status": "active",
                "type": "ltd",
            }
        )
    ).verify_company("12345678", now=datetime(2026, 7, 25, 11, tzinfo=UTC))
    matching_lead = insert_verified_lead(
        conn,
        company=matching_company,
        contact_email="match@example.test",
        source_register="Test register",
    )
    conn.execute("UPDATE fca_firms SET lead_id = ? WHERE id = ?", (matching_lead, firm_id))

    with pytest.raises(sqlite3.IntegrityError, match="company number"):
        conn.execute(
            "UPDATE leads SET company_number = '99999999' WHERE id = ?", (matching_lead,)
        )
