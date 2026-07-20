from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import json
import re
from typing import Any, Mapping


COMPANY_NUMBER = re.compile(r"^[A-Z0-9]{8}$")
INCORPORATED_TYPE_MAP = {
    "ltd": "ltd",
    "plc": "plc",
    "llp": "llp",
    "private-limited-guarant-nsc": "charitable_company",
    "private-limited-guarant-nsc-limited-exemption": "charitable_company",
}
CIC_SUBTYPES = {"community-interest-company", "community-interest-company-limited-by-guarantee"}
_VERIFICATION_RECEIPT = object()


class CompanyNotEligible(ValueError):
    """Raised when Companies House evidence does not prove an eligible entity."""


@dataclass(frozen=True, slots=True)
class VerifiedCompany:
    company_number: str
    legal_name: str
    legal_form: str
    company_status: str
    verified_at: datetime
    verification_source: str
    profile_hash: str
    _proof: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._proof is not _VERIFICATION_RECEIPT:
            raise TypeError(
                "VerifiedCompany requires a Companies House verification receipt"
            )


def verified_company_from_profile(
    profile: Mapping[str, Any], *, now: datetime
) -> VerifiedCompany:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    company_number = str(profile.get("company_number", "")).strip().upper()
    legal_name = str(profile.get("company_name", "")).strip()
    company_status = str(profile.get("company_status", "")).strip().lower()
    company_type = str(profile.get("type", "")).strip().lower()
    company_subtype = str(profile.get("subtype", "")).strip().lower()

    if not COMPANY_NUMBER.fullmatch(company_number):
        raise CompanyNotEligible("Companies House company number is invalid")
    if not legal_name:
        raise CompanyNotEligible("Companies House legal name is missing")
    if company_status != "active":
        raise CompanyNotEligible("Companies House company is not active")
    if company_subtype in CIC_SUBTYPES:
        legal_form = "cic"
    else:
        legal_form = INCORPORATED_TYPE_MAP.get(company_type)
    if legal_form is None:
        raise CompanyNotEligible("Companies House type is not an eligible incorporated form")

    canonical_profile = json.dumps(
        dict(profile), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return VerifiedCompany(
        company_number=company_number,
        legal_name=legal_name,
        legal_form=legal_form,
        company_status=company_status,
        verified_at=now.astimezone(UTC),
        verification_source="companies_house_api",
        profile_hash=hashlib.sha256(canonical_profile).hexdigest(),
        _proof=_VERIFICATION_RECEIPT,
    )
