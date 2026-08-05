from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import sqlite3

from govscout.enrichment import EnrichmentResult, SiteTransport, run_enrichment
from govscout.fca_pipeline import CompanyVerifier, VerificationResult, verify_firm
from govscout.quality import QcResult, run_qc


@dataclass(frozen=True, slots=True)
class ProcessingResult:
    verification: VerificationResult
    enrichment: EnrichmentResult
    qc: QcResult


def process_firm(
    conn: sqlite3.Connection,
    *,
    firm_id: int,
    companies_house: CompanyVerifier,
    site_transport: SiteTransport,
    now: datetime,
) -> ProcessingResult:
    verification = verify_firm(
        conn,
        firm_id=firm_id,
        companies_house=companies_house,
        now=now,
    )
    enrichment = run_enrichment(
        conn,
        firm_id=firm_id,
        transport=site_transport,
        now=now,
    )
    qc = run_qc(conn, firm_id=firm_id, now=now)
    return ProcessingResult(verification, enrichment, qc)
