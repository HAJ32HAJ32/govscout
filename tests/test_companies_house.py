from datetime import UTC, datetime

import pytest

from govscout.companies_house import CompanyNotEligible, verified_company_from_profile


def test_active_companies_house_profile_derives_incorporated_legal_form():
    verified = verified_company_from_profile(
        {
            "company_number": "12345678",
            "company_name": "Example Governance Ltd",
            "company_status": "active",
            "type": "ltd",
        },
        now=datetime(2026, 7, 20, 9, 0, tzinfo=UTC),
    )

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
    with pytest.raises(CompanyNotEligible):
        verified_company_from_profile(
            profile,
            now=datetime(2026, 7, 20, 9, 0, tzinfo=UTC),
        )
