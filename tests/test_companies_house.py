from datetime import UTC, datetime

import pytest

import govscout.companies_house as companies_house
from govscout.companies_house import CompanyNotEligible, CompaniesHouseClient
from tests.support import StubCompaniesHouseTransport


def test_active_companies_house_profile_derives_incorporated_legal_form():
    profile = {
        "company_number": "12345678",
        "company_name": "Example Governance Ltd",
        "company_status": "active",
        "type": "ltd",
    }
    transport = StubCompaniesHouseTransport(profile)

    verified = CompaniesHouseClient(transport).verify_company(
        "12345678",
        now=datetime(2026, 7, 20, 9, 0, tzinfo=UTC),
    )

    assert transport.requests == ["12345678"]
    assert verified.company_number == "12345678"
    assert verified.legal_name == "Example Governance Ltd"
    assert verified.legal_form == "ltd"
    assert verified.company_status == "active"
    assert verified.verification_source == "companies_house_api"
    assert len(verified.profile_hash) == 64


@pytest.mark.parametrize(
    "profile",
    [
        {
            "company_number": "SOLE-001",
            "company_name": "Example Sole Trader",
            "company_status": "active",
            "type": "sole-trader",
        },
        {
            "company_number": "12345678",
            "company_name": "Dissolved Ltd",
            "company_status": "dissolved",
            "type": "ltd",
        },
        {
            "company_number": "not valid",
            "company_name": "Invalid Number Ltd",
            "company_status": "active",
            "type": "ltd",
        },
    ],
)
def test_unincorporated_inactive_or_invalid_profile_is_rejected(profile):
    transport = StubCompaniesHouseTransport(profile)
    company_number = str(profile["company_number"])

    with pytest.raises(CompanyNotEligible):
        CompaniesHouseClient(transport).verify_company(
            company_number,
            now=datetime(2026, 7, 20, 9, 0, tzinfo=UTC),
        )


def test_client_rejects_profile_for_a_different_requested_company():
    transport = StubCompaniesHouseTransport(
        {
            "company_number": "87654321",
            "company_name": "Wrong Company Ltd",
            "company_status": "active",
            "type": "ltd",
        }
    )

    with pytest.raises(CompanyNotEligible, match="does not match"):
        CompaniesHouseClient(transport).verify_company(
            "12345678",
            now=datetime(2026, 7, 20, 9, 0, tzinfo=UTC),
        )


def test_module_does_not_expose_raw_profile_mint_or_receipt():
    assert not hasattr(companies_house, "verified_company_from_profile")
    assert not hasattr(companies_house, "_verified_company_from_retrieved_profile")
    assert not hasattr(companies_house, "_VERIFICATION_RECEIPT")
