from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from govscout.config import load_settings
from govscout.db import connect_database, insert_verified_lead, migrate
from govscout.draft_service import DraftOutcomeUncertain, DraftService
from govscout.policy import PolicyResult
from govscout.sendguard import ReservationRequest, SendGuard
from govscout.web.app import create_app
from tests.support import (
    verified_company_from_test_profile as verified_company_from_profile,
)

ROOT = Path(__file__).resolve().parents[1]


class AllowPolicy:
    def evaluate(self, request):
        return PolicyResult(passed=True)


class SequentialGmailDrafts:
    def __init__(self):
        self.calls = []

    def find_by_ledger_id(self, ledger_id):
        return None

    def create_draft(self, **kwargs):
        self.calls.append(kwargs)
        ledger_id = kwargs["ledger_id"]
        return {
            "draft_id": f"draft-{ledger_id}",
            "message_id": f"message-{ledger_id}",
            "thread_id": f"thread-{ledger_id}",
        }


class StaticCandidates:
    def __init__(self, requests):
        self.requests = requests

    def get(self, lead_id):
        return next(request for request in self.requests if request.lead_id == lead_id)

    def due(self):
        return list(self.requests)


class UncertainDraftService:
    def create_review_draft(self, conn, request, *, now):
        raise DraftOutcomeUncertain("manual reconciliation required")


class ReturningDraftService:
    def __init__(self, *, created):
        self.created = created

    def create_review_draft(self, conn, request, *, now):
        return SimpleNamespace(
            created=self.created,
            draft_id="draft-1",
            send_id=1,
        )


def test_today_omits_drafting_presentation_without_loading_draft_work(tmp_path, monkeypatch):
    database = tmp_path / "govscout.sqlite3"
    conn = connect_database(database)
    migrate(conn)
    conn.close()
    settings = load_settings(ROOT / "config/default.toml")
    monkeypatch.setattr(
        SendGuard,
        "status",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("draft quota was loaded")
        ),
    )

    class NoDraftCandidates:
        def get(self, _lead_id: int) -> ReservationRequest:
            raise AssertionError("draft candidate was loaded")

        def due(self) -> list[ReservationRequest]:
            raise AssertionError("draft candidates were loaded")

    app = create_app(
        conn_factory=lambda: connect_database(database),
        guard=SendGuard(settings),
        candidate_source=NoDraftCandidates(),
        now_provider=lambda: datetime(2026, 7, 21, 8, 30, tzinfo=UTC),
    )
    app.testing = True

    response = app.test_client().get("/today")

    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "Drafts today" not in page
    assert "Email drafting" not in page
    assert "Email drafts" not in page
    assert "LINT_NOT_READY" not in page


def test_today_does_not_hide_review_ready_firm_behind_fifty_research_items(
    tmp_path, monkeypatch
):
    database = tmp_path / "govscout.sqlite3"
    conn = connect_database(database)
    migrate(conn)
    for index in range(51):
        frn = f"{100000 + index}"
        conn.execute(
            """
            INSERT INTO fca_firms (
                frn, firm_name, fca_status, is_active, source_url,
                source_record_hash, first_seen_at, last_seen_at
            ) VALUES (?, ?, 'Authorised', 1, ?, ?, ?, ?)
            """,
            (
                frn,
                f"Research Firm {index:02d}",
                f"https://register.fca.org.uk/s/firm?id={frn}",
                f"{index:064x}",
                "2026-08-05T09:00:00+00:00",
                "2026-08-05T09:00:00+00:00",
            ),
        )
    ready_id = conn.execute(
        """
        INSERT INTO fca_firms (
            frn, firm_name, fca_status, is_active, source_url,
            source_record_hash, first_seen_at, last_seen_at
        ) VALUES ('200000', 'Ready Review Ltd', 'Authorised', 1, ?, ?, ?, ?)
        """,
        (
            "https://register.fca.org.uk/s/firm?id=200000",
            "f" * 64,
            "2026-08-05T09:00:00+00:00",
            "2026-08-05T09:00:00+00:00",
        ),
    ).lastrowid
    run_id = conn.execute(
        """
        INSERT INTO enrichment_runs (
            firm_id, state, started_at, completed_at, input_hash, score, temperature
        ) VALUES (?, 'complete', ?, ?, ?, 0, 'COOL')
        """,
        (
            ready_id,
            "2026-08-05T09:00:00+00:00",
            "2026-08-05T09:01:00+00:00",
            "a" * 64,
        ),
    ).lastrowid
    qc_id = conn.execute(
        """
        INSERT INTO qc_runs (
            firm_id, enrichment_run_id, state, reason_codes, input_hash,
            checked_at, expires_at
        ) VALUES (?, ?, 'fail', '["TEST"]', ?, ?, ?)
        """,
        (
            ready_id,
            run_id,
            "b" * 64,
            "2026-08-05T09:02:00+00:00",
            "2026-08-06T09:02:00+00:00",
        ),
    ).lastrowid
    conn.close()
    monkeypatch.setattr(
        "govscout.web.app.qc_is_current",
        lambda _conn, *, firm_id, qc_run_id, now: (
            firm_id == ready_id and qc_run_id == qc_id
        ),
    )
    app = create_app(
        conn_factory=lambda: connect_database(database),
        guard=SendGuard(load_settings(ROOT / "config/default.toml")),
        now_provider=lambda: datetime(2026, 8, 5, 10, tzinfo=UTC),
    )

    page = app.test_client().get("/today").get_data(as_text=True)

    assert "Ready Review Ltd" in page
    assert f'action="/today/review/{ready_id}"' in page


def test_today_shows_branded_fca_evidence_score_and_review_controls(tmp_path):
    database = tmp_path / "govscout.sqlite3"
    conn = connect_database(database)
    migrate(conn)
    source_url = "https://register.fca.org.uk/s/firm?id=123456"
    firm_id = conn.execute(
        """
        INSERT INTO fca_firms (
            frn, firm_name, fca_status, firm_type, is_active, source_url,
            website_url, source_location, company_number, source_record_hash,
            first_seen_at, last_seen_at
        ) VALUES ('123456', 'Example Finance Ltd', 'Authorised', 'Regulated firm',
            1, ?, 'https://example.test/', 'London', '12345678', ?, ?, ?)
        """,
        (
            source_url,
            "a" * 64,
            "2026-07-25T10:00:00+00:00",
            "2026-07-25T10:00:00+00:00",
        ),
    ).lastrowid
    run_id = conn.execute(
        """
        INSERT INTO enrichment_runs (
            firm_id, state, started_at, website_url, input_hash
        ) VALUES (?, 'running', ?, 'https://example.test/', ?)
        """,
        (firm_id, "2026-07-25T10:00:00+00:00", "a" * 64),
    ).lastrowid
    conn.execute(
        """
        INSERT INTO evidence_items (
            run_id, signal_group, code, evidence_state, weight,
            source_url, excerpt, observed_at, content_hash
        ) VALUES (?, 'ai_exposure', 'AI_VISIBLE', 'present', 30,
            'https://example.test/', 'AI-powered assistant', ?, ?)
        """,
        (run_id, "2026-07-25T10:00:00+00:00", "c" * 64),
    )
    conn.execute(
        """
        UPDATE enrichment_runs
        SET state = 'complete', completed_at = ?, final_url = ?, page_hash = ?,
            score = 82, temperature = 'HOT'
        WHERE id = ? AND state = 'running'
        """,
        (
            "2026-07-25T10:01:00+00:00",
            "https://example.test/",
            "b" * 64,
            run_id,
        ),
    )
    conn.close()
    app = create_app(
        conn_factory=lambda: connect_database(database),
        guard=SendGuard(load_settings(ROOT / "config/default.toml")),
    )

    page = app.test_client().get("/today").get_data(as_text=True)

    assert "Review possible firms for MISE" in page
    assert "Needs research" in page
    assert "Example Finance Ltd" in page
    assert "London" in page
    assert 'href="https://register.fca.org.uk/s/search?q=123456&amp;type=Companies"' in page
    assert f'href="{source_url}"' not in page
    assert "123456" in page
    assert "82" in page
    assert "High priority" in page
    assert "AI exposure" in page
    assert "Found" in page
    assert "AI_VISIBLE" not in page
    assert "AI-powered assistant" in page
    assert 'action="/today/review/1"' not in page
    assert 'action="/today/draft/' not in page
    assert '<label>Reason for rejecting<input name="rejection_reason"' not in page


def test_research_firm_can_be_archived_and_restored_with_append_only_events(tmp_path):
    database = tmp_path / "govscout.sqlite3"
    conn = connect_database(database)
    migrate(conn)
    firm_id = conn.execute(
        """
        INSERT INTO fca_firms (
            frn, firm_name, fca_status, is_active, source_url,
            source_record_hash, first_seen_at, last_seen_at
        ) VALUES ('123456', 'Research Firm Ltd', 'Authorised', 1, ?, ?, ?, ?)
        """,
        (
            "https://register.fca.org.uk/s/firm?id=123456",
            "a" * 64,
            "2026-08-05T10:00:00+00:00",
            "2026-08-05T10:00:00+00:00",
        ),
    ).lastrowid
    conn.close()
    app = create_app(
        conn_factory=lambda: connect_database(database),
        guard=SendGuard(load_settings(ROOT / "config/default.toml")),
        now_provider=lambda: datetime(2026, 8, 5, 11, tzinfo=UTC),
    )
    client = app.test_client()
    page = client.get("/today").get_data(as_text=True)
    assert f'action="/today/research/{firm_id}/archive"' in page
    with client.session_transaction() as browser_session:
        token = browser_session["csrf_token"]

    missing_reason = client.post(
        f"/today/research/{firm_id}/archive",
        data={"csrf_token": token, "action": "archive"},
    )
    archived = client.post(
        f"/today/research/{firm_id}/archive",
        data={
            "csrf_token": token,
            "action": "archive",
            "reason": "Outside the current MISE target market",
        },
    )

    assert missing_reason.status_code == 422
    assert archived.status_code == 303
    stale_archive = client.post(
        f"/today/research/{firm_id}/archive",
        data={
            "csrf_token": token,
            "action": "archive",
            "reason": "Duplicate stale click",
        },
    )
    assert stale_archive.status_code == 409
    archived_page = client.get("/today").get_data(as_text=True)
    assert "Archived firms" in archived_page
    assert "Outside the current MISE target market" in archived_page

    restored = client.post(
        f"/today/research/{firm_id}/archive",
        data={
            "csrf_token": token,
            "action": "restore",
            "reason": "Reconsidering after new information",
            "expected_archive_event_id": "1",
        },
    )
    assert restored.status_code == 303
    restored_page = client.get("/today").get_data(as_text=True)
    assert "Outside the current MISE target market" not in restored_page
    verify = connect_database(database)
    events = verify.execute(
        """
        SELECT action, reason, actor, expected_previous_event_id
        FROM firm_archive_events ORDER BY id
        """
    ).fetchall()
    assert [tuple(row) for row in events] == [
        ("archive", "Outside the current MISE target market", "local-operator", None),
        ("restore", "Reconsidering after new information", "local-operator", 1),
    ]
    with pytest.raises(Exception):
        verify.execute("UPDATE firm_archive_events SET action = 'restore' WHERE id = 1")


def test_today_does_not_present_expired_qc_as_passing_or_approvable(tmp_path):
    database = tmp_path / "govscout.sqlite3"
    conn = connect_database(database)
    migrate(conn)
    firm_id = conn.execute(
        """
        INSERT INTO fca_firms (
            frn, firm_name, fca_status, is_active, source_url,
            company_number, source_record_hash, first_seen_at, last_seen_at
        ) VALUES ('123456', 'Expired QC Ltd', 'Authorised', 1, ?,
                  '12345678', ?, ?, ?)
        """,
        (
            "https://register.fca.org.uk/s/firm?id=123456",
            "a" * 64,
            "2026-07-01T10:00:00+00:00",
            "2026-07-01T10:00:00+00:00",
        ),
    ).lastrowid
    verification_id = conn.execute(
        """
        INSERT INTO company_verification_attempts (
            firm_id, company_number, state, reason_code, checked_at,
            fca_source_record_hash, legal_name, legal_form, company_status,
            profile_hash
        ) VALUES (?, '12345678', 'verified', 'VERIFIED', ?, ?,
                  'Expired QC Ltd', 'ltd', 'active', ?)
        """,
        (
            firm_id,
            "2026-07-01T10:00:00+00:00",
            "a" * 64,
            "e" * 64,
        ),
    ).lastrowid
    run_id = conn.execute(
        """
        INSERT INTO enrichment_runs (
            firm_id, state, started_at, completed_at, website_url, final_url,
            input_hash, page_hash, score, temperature
        ) VALUES (?, 'complete', ?, ?, 'https://example.test/',
            'https://example.test/', ?, ?, 50, 'WARM')
        """,
        (
            firm_id,
            "2026-07-01T10:00:00+00:00",
            "2026-07-01T10:01:00+00:00",
            "b" * 64,
            "c" * 64,
        ),
    ).lastrowid
    conn.execute(
        """
        INSERT INTO qc_runs (
            firm_id, enrichment_run_id, state, reason_codes, input_hash,
            checked_at, expires_at, company_verification_attempt_id
        ) VALUES (?, ?, 'pass', '[]', ?, ?, ?, ?)
        """,
        (
            firm_id,
            run_id,
            "d" * 64,
            "2026-07-01T10:02:00+00:00",
            "2026-07-02T10:02:00+00:00",
            verification_id,
        ),
    )
    conn.close()
    app = create_app(
        conn_factory=lambda: connect_database(database),
        guard=SendGuard(load_settings(ROOT / "config/default.toml")),
        now_provider=lambda: datetime(2026, 7, 29, 9, tzinfo=UTC),
    )

    page = app.test_client().get("/today").get_data(as_text=True)

    assert "Needs a fresh check" in page
    assert 'value="approved"' not in page
    assert 'action="/today/review/1"' not in page
    assert "They cannot be approved here" in page
    assert "Checks:</strong>\n            Passed" not in page
    assert "Company number 12345678" in page
    assert "Company not checked yet" not in page


def test_today_shows_append_only_companies_house_history_for_research(tmp_path):
    database = tmp_path / "govscout.sqlite3"
    conn = connect_database(database)
    migrate(conn)
    firm_id = conn.execute(
        """
        INSERT INTO fca_firms (
            frn, firm_name, fca_status, is_active, source_url, company_number,
            source_record_hash, first_seen_at, last_seen_at
        ) VALUES ('123456', 'Example Finance Ltd', 'Authorised', 1, ?,
                  '12345678', ?, ?, ?)
        """,
        (
            "https://register.fca.org.uk/s/firm?id=123456",
            "a" * 64,
            "2026-08-05T10:00:00+00:00",
            "2026-08-05T10:00:00+00:00",
        ),
    ).lastrowid
    conn.execute(
        """
        INSERT INTO company_verification_attempts (
            firm_id, company_number, state, reason_code, checked_at,
            fca_source_record_hash
        ) VALUES (?, '12345678', 'ineligible', 'NAME_MISMATCH', ?, ?)
        """,
        (firm_id, "2026-08-05T10:01:00+00:00", "a" * 64),
    )
    conn.close()
    app = create_app(
        conn_factory=lambda: connect_database(database),
        guard=SendGuard(load_settings(ROOT / "config/default.toml")),
    )

    page = app.test_client().get("/today").get_data(as_text=True)

    assert "Companies House history" in page
    assert "Ineligible" in page
    assert "NAME_MISMATCH" in page


def test_today_rejection_requires_csrf_and_reason_and_stays_outreach_ineligible(tmp_path):
    database = tmp_path / "govscout.sqlite3"
    conn = connect_database(database)
    migrate(conn)
    firm_id = conn.execute(
        """
        INSERT INTO fca_firms (
            frn, firm_name, fca_status, is_active, source_url,
            source_record_hash, first_seen_at, last_seen_at
        ) VALUES ('123456', 'Example Finance Ltd', 'Authorised', 1, ?, ?, ?, ?)
        """,
        (
            "https://register.fca.org.uk/s/firm?id=123456",
            "a" * 64,
            "2026-07-25T10:00:00+00:00",
            "2026-07-25T10:00:00+00:00",
        ),
    ).lastrowid
    conn.close()
    app = create_app(
        conn_factory=lambda: connect_database(database),
        guard=SendGuard(load_settings(ROOT / "config/default.toml")),
        now_provider=lambda: datetime(2026, 7, 25, 11, tzinfo=UTC),
    )
    client = app.test_client()
    client.get("/today")
    with client.session_transaction() as browser_session:
        token = browser_session["csrf_token"]

    missing = client.post(
        f"/today/review/{firm_id}",
        data={"csrf_token": token, "decision": "rejected"},
    )
    accepted = client.post(
        f"/today/review/{firm_id}",
        data={
            "csrf_token": token,
            "decision": "rejected",
            "rejection_reason": "Evidence is not sufficiently specific",
            "notes": "Recheck next quarter",
        },
    )
    revised = client.post(
        f"/today/review/{firm_id}",
        data={
            "csrf_token": token,
            "decision": "rejected",
            "rejection_reason": "Latest decision",
            "notes": "Latest review note",
        },
    )

    assert missing.status_code == 422
    assert accepted.status_code == 303
    assert revised.status_code == 303
    verify = connect_database(database)
    rows = verify.execute(
        """
        SELECT decision, rejection_reason FROM firm_reviews
        WHERE firm_id = ? ORDER BY id
        """,
        (firm_id,),
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("rejected", "Evidence is not sufficiently specific"),
        ("rejected", "Latest decision"),
    ]
    page = client.get("/today").get_data(as_text=True)
    assert "Latest review note" in page
    assert "Recheck next quarter" not in page


def test_today_rejects_non_loopback_host_header(tmp_path):
    database = tmp_path / "govscout.sqlite3"
    conn = connect_database(database)
    migrate(conn)
    conn.close()
    app = create_app(
        conn_factory=lambda: connect_database(database),
        guard=SendGuard(load_settings(ROOT / "config/default.toml")),
    )
    app.testing = True

    response = app.test_client().get("/today", headers={"Host": "attacker.example"})

    assert response.status_code == 400


def test_crafted_draft_post_cannot_bypass_fail_closed_policy(tmp_path):
    database = tmp_path / "govscout.sqlite3"
    conn = connect_database(database)
    migrate(conn)
    conn.close()
    settings = load_settings(ROOT / "config/default.toml")
    app = create_app(
        conn_factory=lambda: connect_database(database),
        guard=SendGuard(settings),
        now_provider=lambda: datetime(2026, 7, 21, 8, 30, tzinfo=UTC),
    )
    app.testing = True

    response = app.test_client().post("/today/draft/999")

    assert response.status_code == 403
    verify = connect_database(database)
    assert verify.execute("SELECT COUNT(*) FROM sends").fetchone()[0] == 0


def test_valid_csrf_token_still_cannot_bypass_fail_closed_policy(tmp_path):
    database = tmp_path / "govscout.sqlite3"
    conn = connect_database(database)
    migrate(conn)
    conn.close()
    settings = load_settings(ROOT / "config/default.toml")
    app = create_app(
        conn_factory=lambda: connect_database(database),
        guard=SendGuard(settings),
        now_provider=lambda: datetime(2026, 7, 21, 8, 30, tzinfo=UTC),
    )
    app.testing = True
    client = app.test_client()
    client.get("/today")
    with client.session_transaction() as session:
        token = session["csrf_token"]

    response = client.post("/today/draft/999", headers={"X-CSRF-Token": token})

    assert response.status_code == 409
    assert response.get_json() == {"error": "LINT_NOT_READY"}


def test_today_reports_ambiguous_draft_outcome_as_controlled_conflict(tmp_path):
    database = tmp_path / "govscout.sqlite3"
    conn = connect_database(database)
    migrate(conn)
    conn.close()
    settings = load_settings(ROOT / "config/default.toml")
    request = ReservationRequest(
        lead_id=1,
        to_email="director@example.test",
        stage=0,
        template="signal-led",
        subject="your privacy notice and AI",
        body="Compliant test copy.",
    )
    app = create_app(
        conn_factory=lambda: connect_database(database),
        guard=SendGuard(settings),
        draft_service=UncertainDraftService(),
        candidate_source=StaticCandidates([request]),
        now_provider=lambda: datetime(2026, 7, 21, 8, 30, tzinfo=UTC),
    )
    app.testing = True
    client = app.test_client()
    client.get("/today")
    with client.session_transaction() as session:
        token = session["csrf_token"]

    response = client.post("/today/draft/1", headers={"X-CSRF-Token": token})

    assert response.status_code == 409
    assert response.get_json() == {
        "error": "draft_conflict",
        "detail": "manual reconciliation required",
    }


@pytest.mark.parametrize(("created", "expected_status"), [(True, 201), (False, 200)])
def test_today_single_draft_reports_created_or_reused_accurately(
    tmp_path,
    created,
    expected_status,
):
    database = tmp_path / "govscout.sqlite3"
    conn = connect_database(database)
    migrate(conn)
    conn.close()
    request = ReservationRequest(
        lead_id=1,
        to_email="director@example.test",
        stage=0,
        template="signal-led",
        subject="subject",
        body="body",
    )
    app = create_app(
        conn_factory=lambda: connect_database(database),
        guard=SendGuard(load_settings(ROOT / "config/default.toml")),
        draft_service=ReturningDraftService(created=created),
        candidate_source=StaticCandidates([request]),
    )
    client = app.test_client()
    client.get("/today")
    with client.session_transaction() as session:
        token = session["csrf_token"]

    response = client.post("/today/draft/1", headers={"X-CSRF-Token": token})

    assert response.status_code == expected_status
    assert response.get_json() == {
        "created": created,
        "draft_id": "draft-1",
        "send_id": 1,
    }


def test_batch_drafts_followups_first_and_rolls_over_at_effective_limit(tmp_path):
    database = tmp_path / "govscout.sqlite3"
    conn = connect_database(database)
    migrate(conn)
    requests = []
    for number in range(1, 8):
        company = verified_company_from_profile(
            {
                "company_number": f"{number:08d}",
                "company_name": f"Example {number} Ltd",
                "company_status": "active",
                "type": "ltd",
            },
            now=datetime(2026, 7, 20, 9, 0, tzinfo=UTC),
        )
        lead_id = insert_verified_lead(
            conn,
            company=company,
            contact_email=f"person{number}@example.test",
            source_register="Test prospect directory",
        )
        requests.append(
            ReservationRequest(
                lead_id=lead_id,
                to_email=f"person{number}@example.test",
                stage=1 if number in {6, 7} else 0,
                template="fu1" if number in {6, 7} else "signal-led",
                subject="your privacy notice and AI",
                body=f"Compliant test body {number}.",
            )
        )
    conn.close()
    settings = load_settings(ROOT / "config/default.toml")
    guard = SendGuard(settings)
    gmail = SequentialGmailDrafts()
    app = create_app(
        conn_factory=lambda: connect_database(database),
        guard=guard,
        draft_service=DraftService(guard=guard, policy=AllowPolicy(), gmail=gmail),
        candidate_source=StaticCandidates(requests),
        now_provider=lambda: datetime(2026, 7, 21, 8, 30, tzinfo=UTC),
    )
    app.testing = True
    client = app.test_client()
    page = client.get("/today").get_data(as_text=True)
    assert "Email drafts" not in page
    assert 'action="/today/drafts"' not in page
    with client.session_transaction() as session:
        token = session["csrf_token"]

    response = client.post("/today/drafts", headers={"X-CSRF-Token": token})
    retry = client.post("/today/drafts", headers={"X-CSRF-Token": token})

    assert response.status_code == 200
    assert response.get_json() == {"drafted": 5, "rolled_over": 2}
    assert retry.status_code == 200
    assert retry.get_json() == {"drafted": 0, "rolled_over": 2}
    assert [call["ledger_id"] for call in gmail.calls] == [1, 2, 3, 4, 5]
    verify = connect_database(database)
    drafted_leads = [
        row[0] for row in verify.execute("SELECT lead_id FROM sends ORDER BY id")
    ]
    assert drafted_leads[:2] == [6, 7]
