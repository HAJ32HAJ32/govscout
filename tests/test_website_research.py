from datetime import timedelta
import sqlite3

import pytest

import govscout.db as db_module
from govscout.db import connect_database, migrate
from govscout.fca_pipeline import verify_firm
from govscout.processing_queue import _claim_next_job, _record_outcome, run_pending_jobs
from govscout.research import ResearchConflict, record_archive_event
from govscout.website_research import (
    WebsiteResearchConflict,
    enqueue_website_reprocessing,
    record_website_evidence,
)
from tests.test_processing_queue import (
    NOW,
    FakeSiteTransport,
    _companies_house,
    _queue_firm,
)


def _verified_firm(conn):
    _queue_firm(conn, website=None)
    firm = conn.execute("SELECT * FROM fca_firms WHERE frn = '123456'").fetchone()
    verification = verify_firm(
        conn,
        firm_id=firm["id"],
        companies_house=_companies_house(),
        now=NOW,
    )
    assert verification.verified is True
    return firm


def test_migration_014_keeps_original_queue_schema_and_rows_unchanged(
    tmp_path, monkeypatch
):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrations = db_module._migration_texts()
    monkeypatch.setattr(
        db_module,
        "_migration_texts",
        lambda: tuple(item for item in migrations if item[0] != "014"),
    )
    migrate(conn)
    firm = _verified_firm(conn)
    before_columns = [
        tuple(row) for row in conn.execute("PRAGMA table_info(fca_processing_jobs)")
    ]
    before_job = tuple(
        conn.execute("SELECT * FROM fca_processing_jobs WHERE firm_id = ?", (firm["id"],)).fetchone()
    )
    before_schema = {
        row["name"]: row["sql"]
        for row in conn.execute(
            """
            SELECT name, sql FROM sqlite_master
            WHERE tbl_name IN ('fca_processing_jobs', 'fca_processing_job_events')
            ORDER BY type, name
            """
        )
    }
    before_events = [
        tuple(row)
        for row in conn.execute(
            "SELECT * FROM fca_processing_job_events ORDER BY id"
        )
    ]

    monkeypatch.setattr(db_module, "_migration_texts", lambda: migrations)
    migrate(conn)

    after_columns = [
        tuple(row) for row in conn.execute("PRAGMA table_info(fca_processing_jobs)")
    ]
    after_job = tuple(
        conn.execute("SELECT * FROM fca_processing_jobs WHERE firm_id = ?", (firm["id"],)).fetchone()
    )
    assert after_columns == before_columns
    assert after_job == before_job
    assert {
        row["name"]: row["sql"]
        for row in conn.execute(
            """
            SELECT name, sql FROM sqlite_master
            WHERE tbl_name IN ('fca_processing_jobs', 'fca_processing_job_events')
            ORDER BY type, name
            """
        )
    } == before_schema
    assert [
        tuple(row)
        for row in conn.execute(
            "SELECT * FROM fca_processing_job_events ORDER BY id"
        )
    ] == before_events
    conn.execute(
        """
        INSERT OR IGNORE INTO fca_processing_jobs (
            firm_id, source_record_hash, state, attempt_count,
            available_at, created_at, updated_at
        ) VALUES (?, ?, 'pending', 0, ?, ?, ?)
        """,
        (firm["id"], firm["source_record_hash"], NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
    )


def test_assertion_and_withdrawal_are_append_only_and_stale_fenced(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    firm = _verified_firm(conn)
    asserted = record_website_evidence(
        conn,
        firm_id=firm["id"],
        action="assert",
        website_url="https://official.example.test/",
        evidence_url="https://register.example.test/firm/123456",
        justification="The source identifies the legal company and this official domain.",
        actor="local-operator",
        expected_previous_event_id=None,
        now=NOW,
    )
    with pytest.raises(WebsiteResearchConflict):
        record_website_evidence(
            conn,
            firm_id=firm["id"],
            action="assert",
            website_url="https://stale.example.test/",
            evidence_url="https://register.example.test/stale",
            justification="This submission was based on a stale browser form.",
            actor="local-operator",
            expected_previous_event_id=None,
            now=NOW,
        )
    withdrawn = record_website_evidence(
        conn,
        firm_id=firm["id"],
        action="withdraw",
        website_url="https://official.example.test/",
        evidence_url="https://register.example.test/firm/123456",
        justification="Later evidence showed that the domain assertion was unsafe.",
        actor="local-operator",
        expected_previous_event_id=asserted,
        now=NOW + timedelta(minutes=1),
    )
    assert [
        tuple(row) for row in conn.execute(
            "SELECT id, action, expected_previous_event_id FROM firm_website_evidence_events ORDER BY id"
        )
    ] == [(asserted, "assert", None), (withdrawn, "withdraw", asserted)]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE firm_website_evidence_events SET action = 'assert' WHERE id = ?", (withdrawn,))


def test_reprocessing_requires_current_verified_identity_and_is_idempotent(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    firm = _verified_firm(conn)
    evidence_id = record_website_evidence(
        conn,
        firm_id=firm["id"],
        action="assert",
        website_url="https://official.example.test/",
        evidence_url="https://official.example.test/legal",
        justification="The legal page identifies the regulated company by name.",
        actor="local-operator",
        expected_previous_event_id=None,
        now=NOW,
    )
    first = enqueue_website_reprocessing(
        conn,
        firm_id=firm["id"],
        expected_website_evidence_event_id=evidence_id,
        requested_by="local-operator",
        request_reason="Use the verified official website for processing.",
        now=NOW,
    )
    second = enqueue_website_reprocessing(
        conn,
        firm_id=firm["id"],
        expected_website_evidence_event_id=evidence_id,
        requested_by="local-operator",
        request_reason="Duplicate browser submission.",
        now=NOW,
    )
    assert first.created is True
    assert second.created is False
    assert second.job_id == first.job_id
    assert conn.execute("SELECT count(*) FROM fca_processing_jobs").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM fca_reprocessing_jobs").fetchone()[0] == 1


def test_database_rejects_mutable_or_mismatched_research_dependencies(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    firm = _verified_firm(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO firm_website_evidence_events (
                firm_id, action, website_url, evidence_url, justification,
                actor, occurred_at, expected_previous_event_id,
                fca_source_record_hash, collector_import_id
            ) VALUES (?, 'assert', 'https://official.example.test/',
                      'https://official.example.test/legal', 'Plausible but mismatched',
                      'local-operator', ?, NULL, ?,
                      (SELECT import_id FROM collector_imports LIMIT 1))
            """,
            (firm["id"], NOW.isoformat(), "f" * 64),
        )
    import_id = conn.execute(
        "SELECT import_id FROM collector_imports LIMIT 1"
    ).fetchone()[0]
    invalid_audit_values = (
        ("Valid justification\x00hidden", "local-operator", NOW.isoformat()),
        ("Valid justification", "local\x00operator", NOW.isoformat()),
        (
            "Valid justification",
            "local-operator",
            "2026-07-29T10:00:00+00:00\x00hidden",
        ),
        ("Valid justification", "local-operator", "2026-07-29T24:00:00+00:00"),
    )
    for justification, actor, occurred_at in invalid_audit_values:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO firm_website_evidence_events (
                    firm_id, action, website_url, evidence_url, justification,
                    actor, occurred_at, expected_previous_event_id,
                    fca_source_record_hash, collector_import_id
                ) VALUES (?, 'assert', 'https://official.example.test/',
                          'https://official.example.test/legal', ?, ?, ?, NULL, ?, ?)
                """,
                (
                    firm["id"], justification, actor, occurred_at,
                    firm["source_record_hash"], import_id,
                ),
            )


def test_reprocessing_database_rejects_invalid_immutable_audit_values(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    firm = _verified_firm(conn)
    evidence_id = record_website_evidence(
        conn,
        firm_id=firm["id"],
        action="assert",
        website_url="https://official.example.test/",
        evidence_url="https://official.example.test/legal",
        justification="Official legal page names the regulated firm.",
        actor="local-operator",
        expected_previous_event_id=None,
        now=NOW + timedelta(minutes=1),
    )
    queued = enqueue_website_reprocessing(
        conn,
        firm_id=firm["id"],
        expected_website_evidence_event_id=evidence_id,
        requested_by="local-operator",
        request_reason="Use the evidenced official website for remediation.",
        now=NOW + timedelta(minutes=2),
    )
    source = dict(
        conn.execute(
            "SELECT * FROM fca_reprocessing_jobs WHERE id = ?", (queued.job_id,)
        ).fetchone()
    )
    columns = (
        "firm_id", "source_job_id", "source_record_hash",
        "website_evidence_event_id", "company_verification_attempt_id",
        "input_hash", "requested_by", "request_reason", "state",
        "attempt_count", "available_at", "claimed_at", "claim_token",
        "completed_at", "outcome_code", "created_at", "updated_at",
    )
    sql = """
        INSERT INTO fca_reprocessing_jobs (
            firm_id, source_job_id, source_record_hash,
            website_evidence_event_id, company_verification_attempt_id,
            input_hash, requested_by, request_reason, state, attempt_count,
            available_at, claimed_at, claim_token, completed_at, outcome_code,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    invalid_values = (
        ("requested_by", "local\x00operator"),
        ("request_reason", "Valid request\x00hidden"),
        ("available_at", "2026-07-29T24:00:00+00:00"),
        ("created_at", "2026-07-29T10:00:00+00:00\x00hidden"),
        ("updated_at", "not-a-timestamp+00:00"),
    )
    for index, (field, invalid) in enumerate(invalid_values, start=1):
        candidate = source.copy()
        candidate["input_hash"] = f"{index:064x}"
        candidate[field] = invalid
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql, tuple(candidate[column] for column in columns))

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO fca_reprocessing_job_events (
                job_id, from_state, to_state, attempt_count, outcome_code, occurred_at
            ) VALUES (?, NULL, 'pending', 0, NULL, ?)
            """,
            (queued.job_id, "2026-07-29T24:00:00+00:00"),
        )


def test_bounded_worker_processes_research_job_without_touching_original_history(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    firm = _verified_firm(conn)
    original = _claim_next_job(conn, now=NOW + timedelta(seconds=2))
    assert original is not None
    _record_outcome(
        conn,
        job=original,
        state="failed",
        outcome_code="WEBSITE_MISSING",
        now=NOW,
    )
    original_events = [
        tuple(row) for row in conn.execute(
            """
            SELECT from_state, to_state, attempt_count, outcome_code
            FROM fca_processing_job_events ORDER BY id
            """
        )
    ]
    evidence_id = record_website_evidence(
        conn,
        firm_id=firm["id"],
        action="assert",
        website_url="https://official.example.test/",
        evidence_url="https://official.example.test/legal",
        justification="The legal page identifies the regulated company by name.",
        actor="local-operator",
        expected_previous_event_id=None,
        now=NOW + timedelta(minutes=1),
    )
    queued = enqueue_website_reprocessing(
        conn,
        firm_id=firm["id"],
        expected_website_evidence_event_id=evidence_id,
        requested_by="local-operator",
        request_reason="Reprocess using the newly verified official website.",
        now=NOW + timedelta(minutes=2),
    )
    transport = FakeSiteTransport(
        {
            "https://official.example.test/":
                "<a href='/contact'>Contact</a><a href='/privacy'>Privacy</a>",
            "https://official.example.test/contact":
                "Example Governance Ltd company number 12345678",
            "https://official.example.test/privacy": "Privacy notice",
        }
    )

    result = run_pending_jobs(
        conn,
        companies_house=_companies_house(),
        site_transport=transport,
        now=NOW + timedelta(minutes=2),
        limit=1,
    )

    assert result.claimed == 1
    assert result.succeeded == 1
    reprocessing = conn.execute(
        """
        SELECT id, state, website_evidence_event_id,
               company_verification_attempt_id, input_hash
        FROM fca_reprocessing_jobs WHERE id = ?
        """,
        (queued.job_id,),
    ).fetchone()
    enrichment = conn.execute(
        """
        SELECT website_url, website_evidence_event_id,
               company_verification_attempt_id, input_hash
        FROM enrichment_runs ORDER BY id DESC LIMIT 1
        """
    ).fetchone()
    qc = conn.execute(
        """
        SELECT website_evidence_event_id, state
        FROM qc_runs ORDER BY id DESC LIMIT 1
        """
    ).fetchone()
    assert tuple(reprocessing[:3]) == (queued.job_id, "succeeded", evidence_id)
    assert tuple(enrichment) == (
        "https://official.example.test/",
        evidence_id,
        reprocessing["company_verification_attempt_id"],
        reprocessing["input_hash"],
    )
    assert tuple(qc) == (evidence_id, "pass")
    assert original_events == [
        tuple(row) for row in conn.execute(
            """
            SELECT from_state, to_state, attempt_count, outcome_code
            FROM fca_processing_job_events ORDER BY id
            """
        )
    ]


def test_evidence_superseded_during_fetch_fails_before_persistence(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    firm = _verified_firm(conn)
    original = _claim_next_job(conn, now=NOW + timedelta(seconds=2))
    assert original is not None
    _record_outcome(
        conn,
        job=original,
        state="failed",
        outcome_code="WEBSITE_MISSING",
        now=NOW,
    )
    evidence_id = record_website_evidence(
        conn,
        firm_id=firm["id"],
        action="assert",
        website_url="https://official.example.test/",
        evidence_url="https://official.example.test/legal",
        justification="The legal page identifies the regulated company by name.",
        actor="local-operator",
        expected_previous_event_id=None,
        now=NOW + timedelta(minutes=1),
    )
    queued = enqueue_website_reprocessing(
        conn,
        firm_id=firm["id"],
        expected_website_evidence_event_id=evidence_id,
        requested_by="local-operator",
        request_reason="Reprocess using the newly verified official website.",
        now=NOW + timedelta(minutes=2),
    )
    base = FakeSiteTransport(
        {
            "https://official.example.test/": "Official home page",
            "https://official.example.test/privacy": "Privacy notice",
        }
    )

    class SupersedingTransport:
        changed = False

        def fetch_html(self, url):
            if not self.changed:
                self.changed = True
                record_website_evidence(
                    conn,
                    firm_id=firm["id"],
                    action="assert",
                    website_url="https://new-official.example.test/",
                    evidence_url="https://new-official.example.test/legal",
                    justification="Newer evidence supersedes the URL being fetched.",
                    actor="local-operator",
                    expected_previous_event_id=evidence_id,
                    now=NOW + timedelta(minutes=3),
                )
            return base.fetch_html(url)

    result = run_pending_jobs(
        conn,
        companies_house=_companies_house(),
        site_transport=SupersedingTransport(),
        now=NOW + timedelta(minutes=2),
        limit=1,
    )

    job = conn.execute(
        "SELECT state, outcome_code FROM fca_reprocessing_jobs WHERE id = ?",
        (queued.job_id,),
    ).fetchone()
    assert result.failed == 1
    assert tuple(job) == ("failed", "REPROCESSING_INPUT_CHANGED")
    assert conn.execute("SELECT count(*) FROM enrichment_runs").fetchone()[0] == 0


def test_verification_replaced_during_fetch_fails_before_persistence(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    firm = _verified_firm(conn)
    original = _claim_next_job(conn, now=NOW + timedelta(seconds=2))
    assert original is not None
    _record_outcome(
        conn,
        job=original,
        state="failed",
        outcome_code="WEBSITE_MISSING",
        now=NOW,
    )
    evidence_id = record_website_evidence(
        conn,
        firm_id=firm["id"],
        action="assert",
        website_url="https://official.example.test/",
        evidence_url="https://official.example.test/legal",
        justification="The legal page identifies the regulated company by name.",
        actor="local-operator",
        expected_previous_event_id=None,
        now=NOW + timedelta(minutes=1),
    )
    queued = enqueue_website_reprocessing(
        conn,
        firm_id=firm["id"],
        expected_website_evidence_event_id=evidence_id,
        requested_by="local-operator",
        request_reason="Reprocess using the newly verified official website.",
        now=NOW + timedelta(minutes=2),
    )
    base = FakeSiteTransport(
        {
            "https://official.example.test/": "Official home page",
            "https://official.example.test/privacy": "Privacy notice",
        }
    )

    class ReverificationTransport:
        changed = False

        def fetch_html(self, url):
            if not self.changed:
                self.changed = True
                result = verify_firm(
                    conn,
                    firm_id=firm["id"],
                    companies_house=_companies_house(),
                    now=NOW + timedelta(minutes=3),
                    force_refresh=True,
                )
                assert result.verified is True
            return base.fetch_html(url)

    result = run_pending_jobs(
        conn,
        companies_house=_companies_house(),
        site_transport=ReverificationTransport(),
        now=NOW + timedelta(minutes=2),
        limit=1,
    )

    job = conn.execute(
        "SELECT state, outcome_code FROM fca_reprocessing_jobs WHERE id = ?",
        (queued.job_id,),
    ).fetchone()
    assert result.failed == 1
    assert tuple(job) == ("failed", "REPROCESSING_INPUT_CHANGED")
    assert conn.execute("SELECT count(*) FROM enrichment_runs").fetchone()[0] == 0


def test_running_reprocessing_job_blocks_archive(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    firm = _verified_firm(conn)
    original = _claim_next_job(conn, now=NOW + timedelta(seconds=2))
    assert original is not None
    _record_outcome(
        conn,
        job=original,
        state="failed",
        outcome_code="WEBSITE_MISSING",
        now=NOW,
    )
    evidence_id = record_website_evidence(
        conn,
        firm_id=firm["id"],
        action="assert",
        website_url="https://official.example.test/",
        evidence_url="https://official.example.test/legal",
        justification="The legal page identifies the regulated company by name.",
        actor="local-operator",
        expected_previous_event_id=None,
        now=NOW + timedelta(minutes=1),
    )
    enqueue_website_reprocessing(
        conn,
        firm_id=firm["id"],
        expected_website_evidence_event_id=evidence_id,
        requested_by="local-operator",
        request_reason="Reprocess using the newly verified official website.",
        now=NOW + timedelta(minutes=2),
    )
    reprocessing = _claim_next_job(conn, now=NOW + timedelta(minutes=2))
    assert reprocessing is not None
    assert reprocessing.queue_name == "fca_reprocessing_jobs"

    with pytest.raises(ResearchConflict, match="running"):
        record_archive_event(
            conn,
            firm_id=firm["id"],
            action="archive",
            reason="Research evidence must be resolved before continuing.",
            actor="local-operator",
            expected_previous_event_id=None,
            now=NOW + timedelta(minutes=3),
        )
    assert conn.execute("SELECT count(*) FROM firm_archive_events").fetchone()[0] == 0
