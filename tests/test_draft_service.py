from datetime import UTC, datetime, timedelta
from dataclasses import replace
from pathlib import Path

import pytest

from govscout.config import load_settings
from govscout.db import connect_database, insert_verified_lead, migrate
from govscout.draft_service import (
    DraftOutcomeUncertain,
    DraftPolicyRefused,
    DraftService,
)
from govscout.enrichment import SitePage, run_enrichment
from govscout.fca_discovery import ingest_fca_records, parse_fca_json
from govscout.policy import LintNotReadyPolicy, PolicyResult
from govscout.quality import review_firm, run_qc
from govscout.sendguard import ReservationRequest, SendGuard
from tests.support import (
    verified_company_from_test_profile as verified_company_from_profile,
)


ROOT = Path(__file__).resolve().parents[1]


class RecordingGmailDrafts:
    def __init__(self):
        self.create_calls = []
        self.delete_calls = []

    def find_by_ledger_id(self, ledger_id):
        return None

    def delete_draft(self, draft_id):
        self.delete_calls.append(draft_id)

    def create_draft(self, **kwargs):
        self.create_calls.append(kwargs)
        return {
            "draft_id": "draft-1",
            "message_id": "message-1",
            "thread_id": "thread-1",
        }


class AllowPolicy:
    def evaluate(self, request):
        return PolicyResult(passed=True)


class FailingGmailDrafts(RecordingGmailDrafts):
    def create_draft(self, **kwargs):
        self.create_calls.append(kwargs)
        raise RuntimeError("simulated Gmail timeout")


class RecoveringGmailDrafts(FailingGmailDrafts):
    def find_by_ledger_id(self, ledger_id):
        return {
            "draft_id": "recovered-draft",
            "message_id": "recovered-message",
            "thread_id": "recovered-thread",
        }


def _setup(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    company = verified_company_from_profile(
        {
            "company_number": "12345678",
            "company_name": "Example Governance Ltd",
            "company_status": "active",
            "type": "ltd",
        },
        now=datetime(2026, 7, 20, 9, 0, tzinfo=UTC),
    )
    lead_id = insert_verified_lead(
        conn,
        company=company,
        contact_email="director@example.test",
        source_register="Test prospect directory",
    )
    request = ReservationRequest(
        lead_id=lead_id,
        to_email="director@example.test",
        stage=0,
        template="signal-led",
        subject="your privacy notice and AI",
        body="A test message that must not reach Gmail before lint is ready.",
    )
    return conn, request


def _link_fca_firm(conn, lead_id):
    records = parse_fca_json(
        b'{"firms":[{"frn":"123456","firm_name":"Example Governance Ltd",'
        b'"status":"Authorised","firm_type":"Regulated firm",'
        b'"source_url":"https://register.fca.org.uk/s/firm?id=123456",'
        b'"website_url":"https://example.test/","location":"London",'
        b'"company_number":"12345678"}]}'
    )
    ingest_fca_records(
        conn,
        records,
        limit=1,
        now=datetime(2026, 7, 21, 8, tzinfo=UTC),
    )
    conn.execute("UPDATE fca_firms SET lead_id = ? WHERE frn = '123456'", (lead_id,))


def _approve_fca_firm(conn, lead_id, *, now):
    _link_fca_firm(conn, lead_id)
    firm_id = conn.execute(
        "SELECT id FROM fca_firms WHERE lead_id = ?", (lead_id,)
    ).fetchone()[0]
    source_hash = conn.execute(
        "SELECT source_record_hash FROM fca_firms WHERE id = ?", (firm_id,)
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO company_verification_attempts (
            firm_id, company_number, state, reason_code, checked_at,
            fca_source_record_hash, legal_name, legal_form, company_status,
            profile_hash
        ) VALUES (?, '12345678', 'verified', 'VERIFIED', ?, ?,
                  'Example Governance Ltd', 'ltd', 'active', ?)
        """,
        (firm_id, now.isoformat(), source_hash, "f" * 64),
    )

    class PassingTransport:
        def fetch_html(self, url):
            pages = {
                "https://example.test/": "FCA regulated. AI-powered services.",
                "https://example.test/privacy": (
                    "Privacy notice with automated decision safeguards."
                ),
                "https://example.test/careers": "Careers for our Copilot-enabled team.",
                "https://example.test/ai-policy": "Our AI governance policy and oversight.",
            }
            return SitePage(
                url=url,
                final_url=url,
                html=pages[url],
                fetched_at=now,
            )

    run_enrichment(conn, firm_id=firm_id, transport=PassingTransport(), now=now)
    qc = run_qc(conn, firm_id=firm_id, now=now)
    assert qc.passed
    review_firm(
        conn,
        firm_id=firm_id,
        decision="approved",
        qc_run_id=qc.qc_run_id,
        notes="Checked",
        rejection_reason=None,
        now=now,
    )
    return firm_id


def test_production_policy_refuses_before_reserving_or_calling_gmail(tmp_path):
    conn, request = _setup(tmp_path)
    gmail = RecordingGmailDrafts()
    service = DraftService(
        guard=SendGuard(load_settings(ROOT / "config/default.toml")),
        policy=LintNotReadyPolicy(),
        gmail=gmail,
    )

    with pytest.raises(DraftPolicyRefused, match="LINT_NOT_READY"):
        service.create_review_draft(
            conn,
            request,
            now=datetime(2026, 7, 21, 8, 30, tzinfo=UTC),
        )

    assert gmail.create_calls == []
    assert conn.execute("SELECT COUNT(*) FROM sends").fetchone()[0] == 0


def test_fca_linked_lead_requires_current_qc_and_explicit_approval_before_reservation(
    tmp_path,
):
    conn, request = _setup(tmp_path)
    _link_fca_firm(conn, request.lead_id)
    gmail = RecordingGmailDrafts()
    service = DraftService(
        guard=SendGuard(load_settings(ROOT / "config/default.toml")),
        policy=AllowPolicy(),
        gmail=gmail,
    )

    with pytest.raises(DraftPolicyRefused, match="FCA_QC_APPROVAL_REQUIRED"):
        service.create_review_draft(
            conn,
            request,
            now=datetime(2026, 7, 21, 8, 30, tzinfo=UTC),
        )

    assert gmail.create_calls == []
    assert conn.execute("SELECT COUNT(*) FROM sends").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM app_state").fetchone()[0] == 0


def test_fca_rejection_at_reservation_boundary_prevents_ledger_and_gmail(tmp_path):
    conn, request = _setup(tmp_path)
    now = datetime(2026, 7, 21, 8, 30, tzinfo=UTC)
    firm_id = _approve_fca_firm(conn, request.lead_id, now=now)
    gmail = RecordingGmailDrafts()

    class RejectingAtReservationGuard(SendGuard):
        def reserve(self, conn, request, *, now, validator=None):
            review_firm(
                conn,
                firm_id=firm_id,
                decision="rejected",
                qc_run_id=None,
                notes="Boundary review",
                rejection_reason="Withdrawn immediately before reservation",
                now=now + timedelta(seconds=1),
            )
            if validator is None:
                return super().reserve(conn, request, now=now)
            return super().reserve(conn, request, now=now, validator=validator)

    service = DraftService(
        guard=RejectingAtReservationGuard(load_settings(ROOT / "config/default.toml")),
        policy=AllowPolicy(),
        gmail=gmail,
    )

    with pytest.raises(DraftPolicyRefused, match="FCA_QC_APPROVAL_REQUIRED"):
        service.create_review_draft(conn, request, now=now)

    assert gmail.create_calls == []
    assert conn.execute("SELECT COUNT(*) FROM sends").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM app_state").fetchone()[0] == 0


def test_passing_policy_creates_plain_review_draft_and_finalises_ledger(tmp_path):
    conn, request = _setup(tmp_path)
    gmail = RecordingGmailDrafts()
    settings = load_settings(ROOT / "config/default.toml")
    service = DraftService(
        guard=SendGuard(settings),
        policy=AllowPolicy(),
        gmail=gmail,
    )

    result = service.create_review_draft(
        conn,
        request,
        now=datetime(2026, 7, 21, 8, 30, tzinfo=UTC),
    )

    assert result.draft_id == "draft-1"
    assert gmail.create_calls == [
        {
            "to_email": "director@example.test",
            "from_email": "harrison@misegroup.co.uk",
            "from_name": "Harrison — Mise",
            "subject": "your privacy notice and AI",
            "body": request.body,
            "ledger_id": result.send_id,
        }
    ]
    row = conn.execute(
        "SELECT state, gmail_draft_id, gmail_message_id, gmail_thread_id FROM sends"
    ).fetchone()
    assert tuple(row) == ("draft", "draft-1", "message-1", "thread-1")


def test_orchestration_passes_only_canonical_recipient_to_gmail(tmp_path):
    conn, request = _setup(tmp_path)
    gmail = RecordingGmailDrafts()
    service = DraftService(
        guard=SendGuard(load_settings(ROOT / "config/default.toml")),
        policy=AllowPolicy(),
        gmail=gmail,
    )

    result = service.create_review_draft(
        conn,
        replace(request, to_email=" Director@Example.Test "),
        now=datetime(2026, 7, 21, 8, 30, tzinfo=UTC),
    )

    assert gmail.create_calls[0]["to_email"] == "director@example.test"
    assert conn.execute(
        "SELECT to_email FROM sends WHERE id = ?", (result.send_id,)
    ).fetchone()[0] == "director@example.test"


def test_ambiguous_gmail_failure_keeps_reservation_counted_and_records_error(tmp_path):
    conn, request = _setup(tmp_path)
    gmail = FailingGmailDrafts()
    service = DraftService(
        guard=SendGuard(load_settings(ROOT / "config/default.toml")),
        policy=AllowPolicy(),
        gmail=gmail,
    )

    with pytest.raises(RuntimeError, match="simulated Gmail timeout"):
        service.create_review_draft(
            conn,
            request,
            now=datetime(2026, 7, 21, 8, 30, tzinfo=UTC),
        )

    row = conn.execute("SELECT state, failure_reason FROM sends").fetchone()
    assert tuple(row) == ("reserved", "Gmail draft outcome uncertain (RuntimeError)")


def test_retry_of_completed_draft_reuses_ledger_without_creating_duplicate(tmp_path):
    conn, request = _setup(tmp_path)
    gmail = RecordingGmailDrafts()
    service = DraftService(
        guard=SendGuard(load_settings(ROOT / "config/default.toml")),
        policy=AllowPolicy(),
        gmail=gmail,
    )
    now = datetime(2026, 7, 21, 8, 30, tzinfo=UTC)

    first = service.create_review_draft(conn, request, now=now)
    retry = service.create_review_draft(conn, request, now=now)

    assert retry.send_id == first.send_id
    assert retry.draft_id == first.draft_id
    assert first.created is True
    assert retry.created is False
    assert len(gmail.create_calls) == 1
    assert conn.execute("SELECT COUNT(*) FROM sends").fetchone()[0] == 1


def test_retry_reconciles_ambiguous_outcome_by_ledger_id(tmp_path):
    conn, request = _setup(tmp_path)
    gmail = RecoveringGmailDrafts()
    service = DraftService(
        guard=SendGuard(load_settings(ROOT / "config/default.toml")),
        policy=AllowPolicy(),
        gmail=gmail,
    )
    now = datetime(2026, 7, 21, 8, 30, tzinfo=UTC)

    with pytest.raises(RuntimeError, match="simulated Gmail timeout"):
        service.create_review_draft(conn, request, now=now)
    recovered = service.create_review_draft(conn, request, now=now)

    assert recovered.draft_id == "recovered-draft"
    assert recovered.created is False
    assert len(gmail.create_calls) == 1
    row = conn.execute("SELECT state, gmail_draft_id FROM sends").fetchone()
    assert tuple(row) == ("draft", "recovered-draft")


def test_unresolved_ambiguous_outcome_stays_reserved_without_second_create(tmp_path):
    conn, request = _setup(tmp_path)
    gmail = FailingGmailDrafts()
    service = DraftService(
        guard=SendGuard(load_settings(ROOT / "config/default.toml")),
        policy=AllowPolicy(),
        gmail=gmail,
    )
    now = datetime(2026, 7, 21, 8, 30, tzinfo=UTC)

    with pytest.raises(RuntimeError, match="simulated Gmail timeout"):
        service.create_review_draft(conn, request, now=now)
    with pytest.raises(DraftOutcomeUncertain, match="no duplicate was created"):
        service.create_review_draft(conn, request, now=now)

    assert len(gmail.create_calls) == 1
    assert conn.execute("SELECT state FROM sends").fetchone()[0] == "reserved"


def test_undo_deletes_gmail_draft_then_voids_but_preserves_ledger_row(tmp_path):
    conn, request = _setup(tmp_path)
    gmail = RecordingGmailDrafts()
    service = DraftService(
        guard=SendGuard(load_settings(ROOT / "config/default.toml")),
        policy=AllowPolicy(),
        gmail=gmail,
    )
    now = datetime(2026, 7, 21, 8, 30, tzinfo=UTC)
    result = service.create_review_draft(conn, request, now=now)

    service.undo_draft(conn, send_id=result.send_id, now=now)

    assert gmail.delete_calls == ["draft-1"]
    row = conn.execute("SELECT id, state, voided_at FROM sends").fetchone()
    assert row[0] == result.send_id
    assert row[1] == "void"
    assert row[2].endswith("+00:00")
