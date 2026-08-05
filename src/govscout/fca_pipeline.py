from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import sqlite3
from typing import Protocol

from govscout.companies_house import CompanyNotEligible, VerifiedCompany
from govscout.companies_house_http import CompaniesHouseTransportError
from govscout.db import insert_verified_lead


class FcaEligibilityError(ValueError):
    """FCA and Companies House evidence does not safely identify one eligible firm."""


class CompanyVerifier(Protocol):
    def verify_company(self, company_number: str, *, now: datetime) -> VerifiedCompany: ...


def _canonical_name(value: str) -> str:
    return " ".join(value.casefold().split())


@dataclass(frozen=True, slots=True)
class VerificationResult:
    attempt_id: int
    verified: bool
    reused: bool
    company: VerifiedCompany | None = None


_VERIFICATION_REUSE_AGE = timedelta(days=30)
_TRANSPORT_REASON_CODES = frozenset(
    {
        "AUTHENTICATION_FAILED",
        "INVALID_CONTENT_TYPE",
        "INVALID_JSON",
        "INVALID_PROFILE",
        "NOT_FOUND",
        "REDIRECT_REFUSED",
        "REQUEST_REFUSED",
        "RESPONSE_TOO_LARGE",
        "TEMPORARILY_UNAVAILABLE",
        "UNEXPECTED_STATUS",
    }
)


def _firm_snapshot(conn: sqlite3.Connection, firm_id: int) -> sqlite3.Row:
    firm = conn.execute("SELECT * FROM fca_firms WHERE id = ?", (firm_id,)).fetchone()
    if firm is None:
        raise FcaEligibilityError("FCA firm does not exist")
    if firm["is_active"] != 1:
        raise FcaEligibilityError("FCA firm is not active")
    if firm["company_number"] is None:
        raise FcaEligibilityError("FCA evidence has no Companies House company number")
    return firm


def _same_firm_snapshot(current: sqlite3.Row, original: sqlite3.Row) -> bool:
    fields = (
        "frn",
        "firm_name",
        "fca_status",
        "firm_type",
        "is_active",
        "source_url",
        "website_url",
        "source_location",
        "company_number",
        "source_record_hash",
        "first_seen_at",
        "last_seen_at",
        "lead_id",
    )
    return all(current[field] == original[field] for field in fields)


def verify_firm(
    conn: sqlite3.Connection,
    *,
    firm_id: int,
    companies_house: CompanyVerifier,
    now: datetime,
    force_refresh: bool = False,
) -> VerificationResult:
    """Verify legal identity without requiring or inventing an outreach contact."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if conn.in_transaction:
        raise sqlite3.OperationalError("firm verification requires no active transaction")
    current = now.astimezone(UTC)
    firm = _firm_snapshot(conn, firm_id)
    if not force_refresh:
        existing = conn.execute(
            """
            SELECT id, state, company_number, fca_source_record_hash, checked_at
            FROM company_verification_attempts
            WHERE firm_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (firm_id,),
        ).fetchone()
        if (
            existing is not None
            and existing["state"] == "verified"
            and existing["company_number"] == firm["company_number"]
            and existing["fca_source_record_hash"] == firm["source_record_hash"]
        ):
            checked_at = datetime.fromisoformat(existing["checked_at"]).astimezone(UTC)
            if checked_at <= current and current - checked_at <= _VERIFICATION_REUSE_AGE:
                return VerificationResult(int(existing["id"]), True, True)

    state = "verified"
    reason_code = "VERIFIED"
    company = None
    caught: Exception | None = None
    try:
        company = companies_house.verify_company(firm["company_number"], now=current)
        if company.company_number != firm["company_number"]:
            reason_code = "COMPANY_NUMBER_MISMATCH"
            raise FcaEligibilityError(
                "Companies House company number does not match FCA snapshot"
            )
        if _canonical_name(company.legal_name) != _canonical_name(firm["firm_name"]):
            reason_code = "LEGAL_NAME_MISMATCH"
            raise FcaEligibilityError("Companies House legal name does not match FCA firm name")
    except CompaniesHouseTransportError as exc:
        state = "error"
        reported_reason = str(exc)
        reason_code = (
            reported_reason
            if reported_reason in _TRANSPORT_REASON_CODES
            else "TRANSPORT_ERROR"
        )
        caught = exc
    except CompanyNotEligible as exc:
        state = "ineligible"
        reason_code = "COMPANY_NOT_ELIGIBLE"
        caught = exc
    except FcaEligibilityError as exc:
        state = "ineligible"
        caught = exc
    else:
        caught = None

    try:
        conn.execute("BEGIN IMMEDIATE")
        refreshed = conn.execute(
            "SELECT * FROM fca_firms WHERE id = ?", (firm_id,)
        ).fetchone()
        if refreshed is None or not _same_firm_snapshot(refreshed, firm):
            raise FcaEligibilityError("FCA eligibility snapshot changed during verification")
        values = (
            firm_id,
            firm["company_number"],
            state,
            reason_code,
            current.isoformat(),
            firm["source_record_hash"],
            company.legal_name if state == "verified" and company is not None else None,
            company.legal_form if state == "verified" and company is not None else None,
            company.company_status if state == "verified" and company is not None else None,
            company.profile_hash if state == "verified" and company is not None else None,
        )
        attempt_id = conn.execute(
            """
            INSERT INTO company_verification_attempts (
                firm_id, company_number, state, reason_code, checked_at,
                fca_source_record_hash, legal_name, legal_form, company_status,
                profile_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        ).lastrowid
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    if attempt_id is None:
        raise RuntimeError("SQLite did not return a verification attempt id")
    if caught is not None:
        raise caught
    return VerificationResult(int(attempt_id), True, False, company)


def verify_and_promote_firm(
    conn: sqlite3.Connection,
    *,
    firm_id: int,
    companies_house: CompanyVerifier,
    contact_email: str,
    now: datetime,
) -> int:
    firm = conn.execute(
        """
        SELECT frn, firm_name, fca_status, firm_type, is_active, source_url,
               website_url, source_location, company_number, source_record_hash,
               first_seen_at, last_seen_at, lead_id
        FROM fca_firms WHERE id = ?
        """,
        (firm_id,),
    ).fetchone()
    if firm is None:
        raise FcaEligibilityError("FCA firm does not exist")
    if firm["lead_id"] is not None:
        return int(firm["lead_id"])
    if firm["is_active"] != 1:
        raise FcaEligibilityError("FCA firm is not active")
    company_number = firm["company_number"]
    if company_number is None:
        raise FcaEligibilityError("FCA evidence has no Companies House company number")
    duplicate = conn.execute(
        """
        SELECT id FROM fca_firms
        WHERE id <> ? AND is_active = 1 AND company_number = ?
        LIMIT 1
        """,
        (firm_id, company_number),
    ).fetchone()
    if duplicate is not None:
        raise FcaEligibilityError("AMBIGUOUS_COMPANY_NUMBER")

    verification = verify_firm(
        conn,
        firm_id=firm_id,
        companies_house=companies_house,
        now=now,
        force_refresh=True,
    )
    company = verification.company
    if company is None:
        raise RuntimeError("fresh company verification did not return verified evidence")
    if conn.in_transaction:
        raise sqlite3.OperationalError("firm promotion requires no active transaction")
    try:
        conn.execute("BEGIN IMMEDIATE")
        refreshed = conn.execute(
            """
            SELECT frn, firm_name, fca_status, firm_type, is_active, source_url,
                   website_url, source_location, company_number, source_record_hash,
                   first_seen_at, last_seen_at, lead_id
            FROM fca_firms WHERE id = ?
            """,
            (firm_id,),
        ).fetchone()
        if refreshed is None:
            raise FcaEligibilityError("FCA firm disappeared during verification")
        eligibility_fields = (
            "frn",
            "firm_name",
            "fca_status",
            "firm_type",
            "is_active",
            "source_url",
            "website_url",
            "source_location",
            "company_number",
            "source_record_hash",
            "first_seen_at",
            "last_seen_at",
            "lead_id",
        )
        if any(refreshed[field] != firm[field] for field in eligibility_fields):
            raise FcaEligibilityError("FCA eligibility snapshot changed during verification")
        latest_verification = conn.execute(
            """
            SELECT id, state FROM company_verification_attempts
            WHERE firm_id = ? ORDER BY id DESC LIMIT 1
            """,
            (firm_id,),
        ).fetchone()
        if (
            latest_verification is None
            or latest_verification["id"] != verification.attempt_id
            or latest_verification["state"] != "verified"
        ):
            raise FcaEligibilityError("Companies House verification changed during promotion")
        duplicate = conn.execute(
            """
            SELECT id FROM fca_firms
            WHERE id <> ? AND is_active = 1 AND company_number = ?
            LIMIT 1
            """,
            (firm_id, company_number),
        ).fetchone()
        if duplicate is not None:
            raise FcaEligibilityError("AMBIGUOUS_COMPANY_NUMBER")
        lead_id = insert_verified_lead(
            conn,
            company=company,
            contact_email=contact_email,
            source_register="FCA Financial Services Register",
            now=now,
        )
        conn.execute(
            "UPDATE fca_firms SET lead_id = ? WHERE id = ? AND lead_id IS NULL",
            (lead_id, firm_id),
        )
        conn.execute("COMMIT")
        return lead_id
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
