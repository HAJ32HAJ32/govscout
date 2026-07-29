from __future__ import annotations

from datetime import datetime
import sqlite3
from typing import Protocol

from govscout.companies_house import VerifiedCompany
from govscout.db import insert_verified_lead


class FcaEligibilityError(ValueError):
    """FCA and Companies House evidence does not safely identify one eligible firm."""


class CompanyVerifier(Protocol):
    def verify_company(self, company_number: str, *, now: datetime) -> VerifiedCompany: ...


def _canonical_name(value: str) -> str:
    return " ".join(value.casefold().split())


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

    company = companies_house.verify_company(company_number, now=now)
    if _canonical_name(company.legal_name) != _canonical_name(firm["firm_name"]):
        raise FcaEligibilityError("Companies House legal name does not match FCA firm name")
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
