from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import re
from typing import Any, Mapping, Protocol
from weakref import WeakValueDictionary


COMPANY_NUMBER = re.compile(r"^[A-Z0-9]{8}$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
INCORPORATED_TYPE_MAP = {
    "ltd": "ltd",
    "plc": "plc",
    "llp": "llp",
    "private-limited-guarant-nsc": "charitable_company",
    "private-limited-guarant-nsc-limited-exemption": "charitable_company",
}
CIC_SUBTYPES = {"community-interest-company", "community-interest-company-limited-by-guarantee"}


class CompanyNotEligible(ValueError):
    """Raised when Companies House evidence does not prove an eligible entity."""


class CompaniesHouseTransport(Protocol):
    """Authenticated retrieval boundary for the Companies House profile API."""

    def get_company_profile(self, company_number: str) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True, init=False, weakref_slot=True)
class VerifiedCompany:
    company_number: str
    legal_name: str
    legal_form: str
    company_status: str
    verified_at: datetime
    verification_source: str
    profile_hash: str
    incorporation_date: str | None

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("VerifiedCompany must be minted by CompaniesHouseClient")


def _build_verification_boundary():
    minted: WeakValueDictionary[int, VerifiedCompany] = WeakValueDictionary()

    class _CompaniesHouseClient:
        """Retrieve and verify evidence; callers cannot mint from supplied data."""

        __slots__ = ("_transport",)

        def __init__(self, transport: CompaniesHouseTransport):
            if not callable(getattr(transport, "get_company_profile", None)):
                raise TypeError("transport must retrieve Companies House profiles")
            self._transport = transport

        def verify_company(
            self, company_number: str, *, now: datetime
        ) -> VerifiedCompany:
            requested_number = company_number.strip().upper()
            if not COMPANY_NUMBER.fullmatch(requested_number):
                raise CompanyNotEligible("Companies House company number is invalid")
            profile = self._transport.get_company_profile(requested_number)
            retrieved_number = str(profile.get("company_number", "")).strip().upper()
            if retrieved_number != requested_number:
                raise CompanyNotEligible(
                    "Companies House response does not match the requested company"
                )
            if now.tzinfo is None or now.utcoffset() is None:
                raise ValueError("now must be timezone-aware")
            legal_name = str(profile.get("company_name", "")).strip()
            company_status = str(profile.get("company_status", "")).strip().lower()
            company_type = str(profile.get("type", "")).strip().lower()
            company_subtype = str(profile.get("subtype", "")).strip().lower()
            raw_incorporation_date = profile.get("date_of_creation")
            incorporation_date = (
                raw_incorporation_date
                if isinstance(raw_incorporation_date, str)
                and _ISO_DATE.fullmatch(raw_incorporation_date)
                else None
            )

            if not legal_name:
                raise CompanyNotEligible("Companies House legal name is missing")
            if company_status != "active":
                raise CompanyNotEligible("Companies House company is not active")
            if company_subtype in CIC_SUBTYPES:
                legal_form = "cic"
            else:
                legal_form = INCORPORATED_TYPE_MAP.get(company_type)
            if legal_form is None:
                raise CompanyNotEligible(
                    "Companies House type is not an eligible incorporated form"
                )

            canonical_profile = json.dumps(
                dict(profile),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
            verified = object.__new__(VerifiedCompany)
            values = {
                "company_number": retrieved_number,
                "legal_name": legal_name,
                "legal_form": legal_form,
                "company_status": company_status,
                "verified_at": now.astimezone(UTC),
                "verification_source": "companies_house_api",
                "profile_hash": hashlib.sha256(canonical_profile).hexdigest(),
                "incorporation_date": incorporation_date,
            }
            for field_name, value in values.items():
                object.__setattr__(verified, field_name, value)
            minted[id(verified)] = verified
            return verified

    def _is_verified_company(company: object) -> bool:
        if type(company) is not VerifiedCompany:
            return False
        try:
            return minted.get(id(company)) is company
        except (AttributeError, TypeError):
            return False

    _CompaniesHouseClient.__name__ = "CompaniesHouseClient"
    _CompaniesHouseClient.__qualname__ = "CompaniesHouseClient"
    return _CompaniesHouseClient, _is_verified_company


CompaniesHouseClient, is_verified_company = _build_verification_boundary()
del _build_verification_boundary
