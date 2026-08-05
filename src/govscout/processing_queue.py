from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
import secrets
import sqlite3
from typing import Callable, Iterator

from govscout.companies_house_http import CompaniesHouseTransportError
from govscout.enrichment import SiteFetchError, SiteTransport
from govscout.fca_pipeline import CompanyVerifier, FcaEligibilityError
from govscout.processing import process_firm
from govscout.quality import qc_is_current, run_qc


MAX_JOB_ATTEMPTS = 3
JOB_LEASE = timedelta(minutes=15)
_RETRY_DELAYS = (timedelta(minutes=5), timedelta(minutes=15))
_TRANSIENT_CODES = frozenset(
    {
        "DNS_FAILED",
        "FETCH_FAILED",
        "TEMPORARILY_UNAVAILABLE",
        "TRANSPORT_ERROR",
        "UNEXPECTED_STATUS",
    }
)
_SITE_FAILURE_CODES = frozenset(
    {
        "COMPANIES_HOUSE_VERIFICATION_REQUIRED",
        "COMPANIES_HOUSE_VERIFICATION_CHANGED",
        "DNS_FAILED",
        "FCA_IDENTITY_CHANGED",
        "FETCH_FAILED",
        "INVALID_CONTENT_LENGTH",
        "INVALID_TIMESTAMP",
        "NON_PUBLIC_ADDRESS",
        "NOT_FOUND",
        "REDIRECTED",
        "RESPONSE_TOO_LARGE",
        "UNDECODABLE_CONTENT",
        "UNSAFE_URL",
        "UNSUPPORTED_CONTENT_TYPE",
        "WEBSITE_MISSING",
    }
)
_VERIFICATION_FAILURE_CODES = frozenset(
    {
        "AUTHENTICATION_FAILED",
        "COMPANY_NOT_ELIGIBLE",
        "COMPANY_NUMBER_MISMATCH",
        "INVALID_CONTENT_TYPE",
        "INVALID_JSON",
        "INVALID_PROFILE",
        "LEGAL_NAME_MISMATCH",
        "NOT_FOUND",
        "REDIRECT_REFUSED",
        "REQUEST_REFUSED",
        "RESPONSE_TOO_LARGE",
        "TEMPORARILY_UNAVAILABLE",
        "TRANSPORT_ERROR",
        "UNEXPECTED_STATUS",
    }
)


@dataclass(frozen=True, slots=True)
class QueueRunResult:
    claimed: int
    succeeded: int
    failed: int
    retried: int


@dataclass(frozen=True, slots=True)
class _ClaimedJob:
    job_id: int
    firm_id: int
    source_record_hash: str
    attempt_count: int
    claim_token: str


def _claim_next_job(conn: sqlite3.Connection, *, now: datetime) -> _ClaimedJob | None:
    timestamp = now.astimezone(UTC).isoformat()
    stale_before = (now.astimezone(UTC) - JOB_LEASE).isoformat()
    claim_token = secrets.token_hex(16)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            UPDATE fca_processing_jobs
            SET state = 'pending', claimed_at = NULL, claim_token = NULL,
                available_at = ?,
                outcome_code = 'WORKER_LEASE_EXPIRED', updated_at = ?
            WHERE state = 'running' AND claimed_at <= ? AND attempt_count < ?
            """,
            (timestamp, timestamp, stale_before, MAX_JOB_ATTEMPTS),
        )
        conn.execute(
            """
            UPDATE fca_processing_jobs
            SET state = 'failed', completed_at = ?, outcome_code = 'RETRY_EXHAUSTED',
                updated_at = ?
            WHERE state = 'running' AND claimed_at <= ? AND attempt_count >= ?
            """,
            (timestamp, timestamp, stale_before, MAX_JOB_ATTEMPTS),
        )
        row = conn.execute(
            """
            SELECT id, firm_id, source_record_hash, attempt_count
            FROM fca_processing_jobs
            WHERE state = 'pending' AND available_at <= ? AND attempt_count < ?
            ORDER BY available_at, id
            LIMIT 1
            """,
            (timestamp, MAX_JOB_ATTEMPTS),
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        updated = conn.execute(
            """
            UPDATE fca_processing_jobs
            SET state = 'running', attempt_count = attempt_count + 1,
                claimed_at = ?, claim_token = ?, outcome_code = NULL, updated_at = ?
            WHERE id = ? AND state = 'pending'
            """,
            (timestamp, claim_token, timestamp, row["id"]),
        ).rowcount
        if updated != 1:
            raise sqlite3.OperationalError("processing job claim lost")
        conn.execute("COMMIT")
        return _ClaimedJob(
            int(row["id"]),
            int(row["firm_id"]),
            row["source_record_hash"],
            int(row["attempt_count"]) + 1,
            claim_token,
        )
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _controlled_failure_code(
    conn: sqlite3.Connection,
    *,
    firm_id: int,
    exc: Exception,
) -> str:
    if isinstance(exc, SiteFetchError) and str(exc) in _SITE_FAILURE_CODES:
        return str(exc)
    if isinstance(exc, CompaniesHouseTransportError | FcaEligibilityError):
        attempt = conn.execute(
            """
            SELECT state, reason_code
            FROM company_verification_attempts
            WHERE firm_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (firm_id,),
        ).fetchone()
        if (
            attempt is not None
            and attempt["state"] != "verified"
            and attempt["reason_code"] in _VERIFICATION_FAILURE_CODES
        ):
            return attempt["reason_code"]
        firm = conn.execute(
            "SELECT is_active, company_number, website_url FROM fca_firms WHERE id = ?",
            (firm_id,),
        ).fetchone()
        if firm is None:
            return "FCA_FIRM_MISSING"
        if firm["is_active"] != 1:
            return "FCA_NOT_ACTIVE"
        if firm["company_number"] is None:
            return "FCA_EVIDENCE_INCOMPLETE"
        if firm["website_url"] is None:
            return "WEBSITE_MISSING"
        return "VERIFICATION_FAILED"
    return "PROCESSING_ERROR"


def _record_outcome(
    conn: sqlite3.Connection,
    *,
    job: _ClaimedJob,
    state: str,
    outcome_code: str,
    now: datetime,
    available_at: datetime | None = None,
) -> tuple[str, str]:
    timestamp = now.astimezone(UTC).isoformat()
    try:
        conn.execute("BEGIN IMMEDIATE")
        current_hash = conn.execute(
            "SELECT source_record_hash FROM fca_firms WHERE id = ?",
            (job.firm_id,),
        ).fetchone()
        if current_hash is None or current_hash["source_record_hash"] != job.source_record_hash:
            state = "failed"
            outcome_code = "SOURCE_CHANGED"
            available_at = None
        if state == "succeeded" and outcome_code == "QC_PASS":
            latest_qc = conn.execute(
                "SELECT id FROM qc_runs WHERE firm_id = ? ORDER BY id DESC LIMIT 1",
                (job.firm_id,),
            ).fetchone()
            if latest_qc is None or not qc_is_current(
                conn,
                firm_id=job.firm_id,
                qc_run_id=int(latest_qc["id"]),
                now=now,
            ):
                state = "failed"
                outcome_code = "QC_STALE_BEFORE_COMPLETION"
                available_at = None
        if state == "pending":
            if available_at is None:
                raise ValueError("pending processing outcome requires available_at")
            updated = conn.execute(
                """
                UPDATE fca_processing_jobs
                SET state = 'pending', claimed_at = NULL, claim_token = NULL,
                    completed_at = NULL, outcome_code = ?, available_at = ?,
                    updated_at = ?
                WHERE id = ? AND state = 'running' AND claim_token = ?
                """,
                (
                    outcome_code,
                    available_at.astimezone(UTC).isoformat(),
                    timestamp,
                    job.job_id,
                    job.claim_token,
                ),
            )
        else:
            updated = conn.execute(
                """
                UPDATE fca_processing_jobs
                SET state = ?, completed_at = ?, outcome_code = ?, updated_at = ?
                WHERE id = ? AND state = 'running' AND claim_token = ?
                """,
                (
                    state,
                    timestamp,
                    outcome_code,
                    timestamp,
                    job.job_id,
                    job.claim_token,
                ),
            )
        if updated.rowcount != 1:
            raise sqlite3.OperationalError("processing job completion lost")
        conn.execute("COMMIT")
        return state, outcome_code
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


@contextmanager
def _exclusive_worker_lock(conn: sqlite3.Connection) -> Iterator[None]:
    database_path = conn.execute("PRAGMA database_list").fetchone()["file"]
    if not database_path:
        raise sqlite3.OperationalError("queue processing requires a file-backed database")
    lock_path = Path(database_path).with_name(Path(database_path).name + ".processing.lock")
    lock_file = lock_path.open("a+b")
    try:
        try:
            import fcntl
        except ImportError as exc:
            raise sqlite3.OperationalError(
                "queue processing requires POSIX advisory locks"
            ) from exc
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise sqlite3.OperationalError("another FCA queue worker is active") from exc
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        lock_file.close()


def _run_pending_jobs_locked(
    conn: sqlite3.Connection,
    *,
    companies_house: CompanyVerifier,
    site_transport: SiteTransport,
    now: datetime,
    limit: int = 10,
    now_provider: Callable[[], datetime] | None = None,
) -> QueueRunResult:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if not 1 <= limit <= 25:
        raise ValueError("processing limit must be between 1 and 25")
    if conn.in_transaction:
        raise sqlite3.OperationalError("queue processing requires no active transaction")
    clock = now_provider or (lambda: now)
    claimed = succeeded = failed = retried = 0
    for _index in range(limit):
        job_now = clock()
        if job_now.tzinfo is None or job_now.utcoffset() is None:
            raise ValueError("worker clock must return timezone-aware values")
        job = _claim_next_job(conn, now=job_now)
        if job is None:
            break
        claimed += 1
        firm = conn.execute(
            "SELECT source_record_hash FROM fca_firms WHERE id = ?",
            (job.firm_id,),
        ).fetchone()
        if firm is None or firm["source_record_hash"] != job.source_record_hash:
            _record_outcome(
                conn,
                job=job,
                state="failed",
                outcome_code="SOURCE_CHANGED",
                now=job_now,
            )
            failed += 1
            continue
        try:
            result = process_firm(
                conn,
                firm_id=job.firm_id,
                companies_house=companies_house,
                site_transport=site_transport,
                now=job_now,
            )
        except (
            CompaniesHouseTransportError,
            FcaEligibilityError,
            KeyError,
            SiteFetchError,
            ValueError,
        ) as exc:
            code = _controlled_failure_code(conn, firm_id=job.firm_id, exc=exc)
            try:
                run_qc(conn, firm_id=job.firm_id, now=job_now)
            except (KeyError, ValueError):
                code = "PROCESSING_ERROR"
            if code in _TRANSIENT_CODES and job.attempt_count < MAX_JOB_ATTEMPTS:
                delay = _RETRY_DELAYS[job.attempt_count - 1]
                _record_outcome(
                    conn,
                    job=job,
                    state="pending",
                    outcome_code=code,
                    now=job_now,
                    available_at=job_now + delay,
                )
                retried += 1
            else:
                _record_outcome(
                    conn,
                    job=job,
                    state="failed",
                    outcome_code=code,
                    now=job_now,
                )
                failed += 1
            continue
        outcome = "QC_PASS" if result.qc.passed else "QC_FAIL"
        final_state, _final_outcome = _record_outcome(
            conn,
            job=job,
            state="succeeded",
            outcome_code=outcome,
            now=job_now,
        )
        if final_state == "succeeded":
            succeeded += 1
        else:
            failed += 1
    return QueueRunResult(claimed, succeeded, failed, retried)


def run_pending_jobs(
    conn: sqlite3.Connection,
    *,
    companies_house: CompanyVerifier,
    site_transport: SiteTransport,
    now: datetime,
    limit: int = 10,
    now_provider: Callable[[], datetime] | None = None,
) -> QueueRunResult:
    with _exclusive_worker_lock(conn):
        return _run_pending_jobs_locked(
            conn,
            companies_house=companies_house,
            site_transport=site_transport,
            now=now,
            limit=limit,
            now_provider=now_provider,
        )
