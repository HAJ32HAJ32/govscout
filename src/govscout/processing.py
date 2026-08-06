from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import sqlite3

from govscout.enrichment import EnrichmentResult, SiteTransport, run_enrichment
from govscout.fca_pipeline import CompanyVerifier, VerificationResult, verify_firm
from govscout.quality import QcResult, run_qc
from govscout.website_candidates import (
    WebsiteCandidateProvider,
    auto_confirm_high_confidence_website,
)


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
    website_url: str | None = None,
    website_evidence_event_id: int | None = None,
    company_verification_attempt_id: int | None = None,
    processing_input_hash: str | None = None,
    reprocessing_job_id: int | None = None,
    now: datetime,
    website_candidate_provider: WebsiteCandidateProvider | None = None,
) -> ProcessingResult:
    verification = verify_firm(
        conn,
        firm_id=firm_id,
        companies_house=companies_house,
        now=now,
    )
    if (
        website_candidate_provider is not None
        and website_url is None
        and website_evidence_event_id is None
    ):
        firm = conn.execute(
            "SELECT website_url, firm_name FROM fca_firms WHERE id = ?", (firm_id,)
        ).fetchone()
        if firm is not None and firm["website_url"] is None:
            # Confirming enqueues a reprocessing job (same as a manual confirm click);
            # it does not change what this in-flight job passes to run_enrichment below,
            # so this job may still end WEBSITE_MISSING while the new job scores it next tick.
            auto_confirm_high_confidence_website(
                conn,
                firm_id=firm_id,
                firm_name=firm["firm_name"],
                provider=website_candidate_provider,
                now=now,
            )
    enrichment = run_enrichment(
        conn,
        firm_id=firm_id,
        transport=site_transport,
        website_url=website_url,
        website_evidence_event_id=website_evidence_event_id,
        company_verification_attempt_id=company_verification_attempt_id,
        processing_input_hash=processing_input_hash,
        reprocessing_job_id=reprocessing_job_id,
        now=now,
    )
    qc = run_qc(conn, firm_id=firm_id, now=now)
    return ProcessingResult(verification, enrichment, qc)
