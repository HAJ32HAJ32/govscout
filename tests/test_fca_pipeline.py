from datetime import UTC, datetime

import pytest

from govscout.companies_house import CompaniesHouseClient
from govscout.db import connect_database, insert_verified_lead, migrate
from govscout.fca_discovery import ingest_fca_records, parse_fca_json
from govscout.fca_pipeline import FcaEligibilityError, verify_and_promote_firm
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

    with pytest.raises(FcaEligibilityError, match="changed during verification"):
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
