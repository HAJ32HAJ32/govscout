import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

import govscout.db as db_module
import govscout.processing_queue as processing_queue
from govscout.auth import create_collector_device
from govscout.cli import main
from govscout.collector_imports import process_collector_import
from govscout.companies_house import CompaniesHouseClient
from govscout.db import connect_database, migrate
from govscout.enrichment import SiteFetchError, SitePage
from govscout.processing_queue import (
    _claim_next_job,
    _exclusive_worker_lock,
    _record_outcome,
    run_pending_jobs,
)
from govscout.research import ResearchConflict, record_archive_event
from tests.support import StubCompaniesHouseTransport


NOW = datetime(2026, 8, 5, 12, tzinfo=UTC)


class FakeSiteTransport:
    def __init__(self, pages):
        self.pages = pages

    def fetch_html(self, url):
        value = self.pages.get(url)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise SiteFetchError("NOT_FOUND")
        return SitePage(url=url, final_url=url, html=value, fetched_at=NOW)


def _queue_firm(conn, *, website: str | None = "https://example.test/"):
    credential = create_collector_device(conn, display_name="H Windows PC", now=NOW)
    payload = json.dumps(
        {
            "firms": [
                {
                    "frn": "123456",
                    "firm_name": "Example Finance Ltd",
                    "status": "Authorised",
                    "firm_type": "Regulated firm",
                    "source_url": "https://register.fca.org.uk/s/firm?id=123456",
                    "website_url": website,
                    "location": "London",
                    "company_number": "12345678",
                }
            ]
        },
        separators=(",", ":"),
    )
    conn.execute(
        """
        INSERT INTO collector_imports (
            import_id, device_id, payload_sha256, payload_json, state, received_at
        ) VALUES (?, ?, ?, ?, 'pending', ?)
        """,
        (
            "a" * 32,
            credential.device_id,
            hashlib.sha256(payload.encode()).hexdigest(),
            payload,
            NOW.isoformat(),
        ),
    )
    process_collector_import(
        conn,
        import_id="a" * 32,
        now=NOW + timedelta(seconds=1),
    )


def _companies_house():
    return CompaniesHouseClient(
        StubCompaniesHouseTransport(
            {
                "company_number": "12345678",
                "company_name": "Example Finance Ltd",
                "company_status": "active",
                "type": "ltd",
            }
        )
    )


def _complete_site():
    return FakeSiteTransport(
        {
            "https://example.test/": "AI-powered FCA regulated advice.",
            "https://example.test/privacy": "Privacy and automated decisions.",
            "https://example.test/careers": "Our team uses Copilot.",
            "https://example.test/ai-policy": "Our AI governance policy.",
        }
    )


def test_worker_processes_due_job_once_without_creating_outreach_records(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    _queue_firm(conn)

    first = run_pending_jobs(
        conn,
        companies_house=_companies_house(),
        site_transport=_complete_site(),
        now=NOW + timedelta(seconds=2),
        limit=5,
    )
    second = run_pending_jobs(
        conn,
        companies_house=_companies_house(),
        site_transport=_complete_site(),
        now=NOW + timedelta(seconds=3),
        limit=5,
    )

    assert (first.claimed, first.succeeded, first.failed, first.retried) == (1, 1, 0, 0)
    assert (second.claimed, second.succeeded, second.failed, second.retried) == (0, 0, 0, 0)
    job = conn.execute(
        "SELECT state, attempt_count, outcome_code FROM fca_processing_jobs"
    ).fetchone()
    assert tuple(job) == ("succeeded", 1, "QC_PASS")
    assert conn.execute("SELECT state FROM qc_runs").fetchone()[0] == "pass"
    for table in ("leads", "firm_reviews", "sends"):
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0

    events = conn.execute(
        """
        SELECT from_state, to_state, attempt_count, outcome_code
        FROM fca_processing_job_events ORDER BY id
        """
    ).fetchall()
    assert [tuple(event) for event in events] == [
        (None, "pending", 0, None),
        ("pending", "running", 1, None),
        ("running", "succeeded", 1, "QC_PASS"),
    ]

    with pytest.raises(sqlite3.IntegrityError, match="terminal|transition"):
        conn.execute(
            """
            UPDATE fca_processing_jobs
            SET state = 'running', completed_at = NULL, outcome_code = NULL
            """
        )
    with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
        conn.execute("DELETE FROM fca_processing_jobs")
    with pytest.raises(sqlite3.IntegrityError, match="identity is immutable"):
        conn.execute(
            "UPDATE fca_processing_jobs SET source_record_hash = ?",
            ("b" * 64,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE fca_processing_job_events SET outcome_code = 'QC_FAIL'")
    with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
        conn.execute("DELETE FROM fca_processing_job_events")


def test_worker_does_not_claim_an_archived_firm(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    _queue_firm(conn)
    firm_id = conn.execute("SELECT id FROM fca_firms").fetchone()[0]
    conn.execute(
        """
        INSERT INTO firm_archive_events (
            firm_id, action, reason, expected_previous_event_id, occurred_at
        ) VALUES (?, 'archive', 'Outside current target market', NULL, ?)
        """,
        (firm_id, NOW.isoformat()),
    )

    result = run_pending_jobs(
        conn,
        companies_house=_companies_house(),
        site_transport=_complete_site(),
        now=NOW + timedelta(seconds=2),
        limit=5,
    )

    assert (result.claimed, result.succeeded, result.failed, result.retried) == (0, 0, 0, 0)
    job = conn.execute(
        "SELECT state, attempt_count FROM fca_processing_jobs"
    ).fetchone()
    assert tuple(job) == ("pending", 0)


def test_archive_refuses_a_firm_while_processing_is_running(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    _queue_firm(conn)
    firm_id = conn.execute("SELECT id FROM fca_firms").fetchone()[0]
    assert _claim_next_job(conn, now=NOW + timedelta(seconds=2)) is not None

    with pytest.raises(ResearchConflict, match="processing is currently running"):
        record_archive_event(
            conn,
            firm_id=firm_id,
            action="archive",
            reason="Outside current target market",
            expected_previous_event_id=None,
            now=NOW + timedelta(seconds=3),
        )

    assert conn.execute("SELECT count(*) FROM firm_archive_events").fetchone()[0] == 0


def test_worker_records_permanent_missing_website_and_fail_closed_qc(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    _queue_firm(conn, website=None)

    result = run_pending_jobs(
        conn,
        companies_house=_companies_house(),
        site_transport=_complete_site(),
        now=NOW + timedelta(seconds=2),
        limit=5,
    )

    assert (result.claimed, result.succeeded, result.failed, result.retried) == (1, 0, 1, 0)
    job = conn.execute(
        "SELECT state, attempt_count, outcome_code FROM fca_processing_jobs"
    ).fetchone()
    assert tuple(job) == ("failed", 1, "WEBSITE_MISSING")
    qc = conn.execute("SELECT state, reason_codes FROM qc_runs").fetchone()
    assert qc["state"] == "fail"
    assert "WEBSITE_MISSING" in json.loads(qc["reason_codes"])


def test_worker_retries_transient_site_failure_then_succeeds(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    _queue_firm(conn)
    transient_site = FakeSiteTransport(
        {"https://example.test/": SiteFetchError("FETCH_FAILED")}
    )

    first = run_pending_jobs(
        conn,
        companies_house=_companies_house(),
        site_transport=transient_site,
        now=NOW + timedelta(seconds=2),
        limit=5,
    )
    pending = conn.execute(
        "SELECT state, attempt_count, outcome_code, available_at FROM fca_processing_jobs"
    ).fetchone()
    second = run_pending_jobs(
        conn,
        companies_house=_companies_house(),
        site_transport=_complete_site(),
        now=datetime.fromisoformat(pending["available_at"]),
        limit=5,
    )

    assert (first.claimed, first.retried) == (1, 1)
    assert tuple(pending[:3]) == ("pending", 1, "FETCH_FAILED")
    assert (second.claimed, second.succeeded) == (1, 1)
    completed = conn.execute(
        "SELECT state, attempt_count, outcome_code FROM fca_processing_jobs"
    ).fetchone()
    assert tuple(completed) == ("succeeded", 2, "QC_PASS")


def test_worker_recovers_an_expired_lease_before_claiming_again(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    _queue_firm(conn)
    stale_claim = NOW - timedelta(minutes=31)
    conn.execute(
        """
        UPDATE fca_processing_jobs
        SET state = 'running', attempt_count = 1, claimed_at = ?,
            claim_token = ?, updated_at = ?
        """,
        (stale_claim.isoformat(), "1" * 32, stale_claim.isoformat()),
    )

    result = run_pending_jobs(
        conn,
        companies_house=_companies_house(),
        site_transport=_complete_site(),
        now=NOW,
        limit=1,
    )

    assert (result.claimed, result.succeeded) == (1, 1)
    job = conn.execute(
        "SELECT state, attempt_count, outcome_code FROM fca_processing_jobs"
    ).fetchone()
    assert tuple(job) == ("succeeded", 2, "QC_PASS")


def test_reclaimed_job_rejects_completion_from_expired_owner(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    _queue_firm(conn)
    stale_claim = NOW + timedelta(seconds=2)
    claim_now = stale_claim + timedelta(minutes=16)
    old_claim = _claim_next_job(conn, now=stale_claim)
    assert old_claim is not None
    new_claim = _claim_next_job(conn, now=claim_now)
    assert new_claim is not None
    assert old_claim.claim_token != new_claim.claim_token

    with pytest.raises(sqlite3.OperationalError, match="completion lost"):
        _record_outcome(
            conn,
            job=old_claim,
            state="succeeded",
            outcome_code="QC_PASS",
            now=claim_now,
        )

    _record_outcome(
        conn,
        job=new_claim,
        state="failed",
        outcome_code="PROCESSING_ERROR",
        now=claim_now,
    )
    job = conn.execute(
        "SELECT state, attempt_count, outcome_code FROM fca_processing_jobs"
    ).fetchone()
    assert tuple(job) == ("failed", 2, "PROCESSING_ERROR")


def test_queue_schema_rejects_exhausted_pending_and_unfenced_running_states(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    _queue_firm(conn)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE fca_processing_jobs SET attempt_count = 3")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE fca_processing_jobs SET attempt_count = 2")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            UPDATE fca_processing_jobs
            SET state = 'running', attempt_count = 1, claimed_at = ?,
                claim_token = NULL
            """,
            (NOW.isoformat(),),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            UPDATE fca_processing_jobs
            SET state = 'failed', attempt_count = 1, claimed_at = ?,
                claim_token = ?, completed_at = ?, outcome_code = NULL
            """,
            (NOW.isoformat(), "2" * 32, NOW.isoformat()),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            UPDATE fca_processing_jobs
            SET state = 'running', attempt_count = 1, claimed_at = ?,
                claim_token = ?, outcome_code = 'FETCH_FAILED'
            """,
            (NOW.isoformat(), "3" * 32),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            UPDATE fca_processing_jobs
            SET state = 'succeeded', attempt_count = 1, claimed_at = ?,
                claim_token = ?, completed_at = ?, outcome_code = 'FETCH_FAILED'
            """,
            (NOW.isoformat(), "4" * 32, NOW.isoformat()),
        )


def test_queue_schema_rejects_attempt_count_corruption_across_transitions(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    _queue_firm(conn)
    timestamp = (NOW + timedelta(seconds=2)).isoformat()

    with pytest.raises(sqlite3.IntegrityError, match="illegal.*transition"):
        conn.execute(
            """
            UPDATE fca_processing_jobs
            SET state = 'running', attempt_count = 3, claimed_at = ?,
                claim_token = ?, outcome_code = NULL, updated_at = ?
            """,
            (timestamp, "5" * 32, timestamp),
        )

    claim = _claim_next_job(conn, now=NOW + timedelta(seconds=2))
    assert claim is not None
    with pytest.raises(sqlite3.IntegrityError, match="illegal.*transition"):
        conn.execute(
            """
            UPDATE fca_processing_jobs
            SET state = 'pending', attempt_count = 0, claimed_at = NULL,
                claim_token = NULL, completed_at = NULL, outcome_code = NULL,
                available_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (timestamp, timestamp, claim.job_id),
        )


def test_migration_012_upgrades_populated_011_queue_and_accepts_worker_completion(
    tmp_path, monkeypatch
):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrations = db_module._migration_texts()
    monkeypatch.setattr(db_module, "_migration_texts", lambda: migrations[:11])
    migrate(conn)
    _queue_firm(conn)
    row = conn.execute(
        "SELECT id, firm_id, source_record_hash FROM fca_processing_jobs"
    ).fetchone()
    claimed_at = (NOW + timedelta(seconds=2)).isoformat()
    claim_token = "c" * 32
    conn.execute(
        """
        UPDATE fca_processing_jobs
        SET state = 'running', attempt_count = 1, claimed_at = ?,
            claim_token = ?, outcome_code = NULL, updated_at = ?
        WHERE id = ?
        """,
        (claimed_at, claim_token, claimed_at, row["id"]),
    )
    claim = processing_queue._ClaimedJob(
        int(row["id"]),
        int(row["firm_id"]),
        row["source_record_hash"],
        1,
        claim_token,
    )

    monkeypatch.setattr(db_module, "_migration_texts", lambda: migrations)
    migrate(conn)
    _record_outcome(
        conn,
        job=claim,
        state="failed",
        outcome_code="PROCESSING_ERROR",
        now=NOW + timedelta(seconds=3),
    )

    job = conn.execute(
        "SELECT state, attempt_count, outcome_code FROM fca_processing_jobs"
    ).fetchone()
    events = conn.execute(
        """
        SELECT from_state, to_state, attempt_count, outcome_code
        FROM fca_processing_job_events ORDER BY id
        """
    ).fetchall()
    assert tuple(job) == ("failed", 1, "PROCESSING_ERROR")
    assert [tuple(event) for event in events] == [
        (None, "running", 1, None),
        ("running", "failed", 1, "PROCESSING_ERROR"),
    ]


def test_worker_exhausts_transient_failures_after_three_attempts(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    _queue_firm(conn)
    transient_site = FakeSiteTransport(
        {"https://example.test/": SiteFetchError("FETCH_FAILED")}
    )

    current = NOW + timedelta(seconds=2)
    for _attempt in range(3):
        run_pending_jobs(
            conn,
            companies_house=_companies_house(),
            site_transport=transient_site,
            now=current,
            limit=1,
        )
        row = conn.execute(
            "SELECT state, available_at FROM fca_processing_jobs"
        ).fetchone()
        if row["state"] == "pending":
            current = datetime.fromisoformat(row["available_at"])

    completed = conn.execute(
        "SELECT state, attempt_count, outcome_code FROM fca_processing_jobs"
    ).fetchone()
    assert tuple(completed) == ("failed", 3, "FETCH_FAILED")


def test_worker_does_not_hide_unexpected_programming_errors(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    _queue_firm(conn)
    broken_site = FakeSiteTransport(
        {"https://example.test/": RuntimeError("unexpected implementation fault")}
    )

    with pytest.raises(RuntimeError, match="implementation fault"):
        run_pending_jobs(
            conn,
            companies_house=_companies_house(),
            site_transport=broken_site,
            now=NOW + timedelta(seconds=2),
            limit=1,
        )

    job = conn.execute(
        "SELECT state, attempt_count, outcome_code FROM fca_processing_jobs"
    ).fetchone()
    assert tuple(job) == ("running", 1, None)


def test_worker_does_not_record_qc_pass_after_verification_becomes_non_current(
    tmp_path, monkeypatch
):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    _queue_firm(conn)
    real_process_firm = processing_queue.process_firm

    def process_then_invalidate(*args, **kwargs):
        result = real_process_firm(*args, **kwargs)
        firm = conn.execute(
            "SELECT company_number, source_record_hash FROM fca_firms WHERE id = 1"
        ).fetchone()
        conn.execute(
            """
            INSERT INTO company_verification_attempts (
                firm_id, company_number, state, reason_code, checked_at,
                fca_source_record_hash
            ) VALUES (1, ?, 'error', 'TRANSPORT_ERROR', ?, ?)
            """,
            (
                firm["company_number"],
                (NOW + timedelta(seconds=3)).isoformat(),
                firm["source_record_hash"],
            ),
        )
        return result

    monkeypatch.setattr(processing_queue, "process_firm", process_then_invalidate)

    result = run_pending_jobs(
        conn,
        companies_house=_companies_house(),
        site_transport=_complete_site(),
        now=NOW + timedelta(seconds=2),
        limit=1,
    )

    assert (result.claimed, result.succeeded, result.failed) == (1, 0, 1)
    job = conn.execute(
        "SELECT state, outcome_code FROM fca_processing_jobs"
    ).fetchone()
    assert tuple(job) == ("failed", "QC_STALE_BEFORE_COMPLETION")


def test_worker_rejects_unbounded_limits(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)

    with pytest.raises(ValueError, match="between 1 and 25"):
        run_pending_jobs(
            conn,
            companies_house=_companies_house(),
            site_transport=_complete_site(),
            now=NOW,
            limit=26,
        )


def test_worker_rejects_overlapping_processes_for_the_same_database(tmp_path):
    database = tmp_path / "govscout.sqlite3"
    first = connect_database(database)
    migrate(first)
    second = connect_database(database)

    with _exclusive_worker_lock(first):
        with pytest.raises(sqlite3.OperationalError, match="worker is active"):
            run_pending_jobs(
                second,
                companies_house=_companies_house(),
                site_transport=_complete_site(),
                now=NOW,
                limit=1,
            )


def test_cli_processes_bounded_due_queue(tmp_path, capsys):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    _queue_firm(conn)

    exit_code = main(
        ["process-fca-queue", "--limit", "5"],
        conn=conn,
        now=NOW + timedelta(seconds=2),
        company_verifier=_companies_house(),
        site_transport=_complete_site(),
    )

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == (
        "FCA queue: claimed 1; succeeded 1; failed 0; retried 0"
    )
