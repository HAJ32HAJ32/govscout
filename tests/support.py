from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from govscout.companies_house import CompaniesHouseClient, VerifiedCompany


class StubCompaniesHouseTransport:
    def __init__(self, profile: Mapping[str, Any]):
        self.profile = profile
        self.requests: list[str] = []

    def get_company_profile(self, company_number: str) -> Mapping[str, Any]:
        self.requests.append(company_number)
        return self.profile


def verified_company_from_test_profile(
    profile: Mapping[str, Any], *, now: datetime
) -> VerifiedCompany:
    company_number = str(profile.get("company_number", ""))
    return CompaniesHouseClient(StubCompaniesHouseTransport(profile)).verify_company(
        company_number,
        now=now,
    )
