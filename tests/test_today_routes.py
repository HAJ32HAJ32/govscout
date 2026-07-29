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


def test_today_shows_authoritative_counter_and_fail_closed_lint_lock(tmp_path):
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

    response = app.test_client().get("/today")

    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "Drafts today: 0 / 10 soft / 5 effective hard" in page
    assert "Production drafting locked: LINT_NOT_READY" in page


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

    assert "FCA-first review" in page
    assert "Example Finance Ltd" in page
    assert "London" in page
    assert f'href="{source_url}"' in page
    assert "123456" in page
    assert "82" in page
    assert "HOT" in page
    assert "AI_VISIBLE" in page
    assert "AI-powered assistant" in page
    assert 'action="/today/review/1"' in page
    assert 'action="/today/draft/' not in page


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
    assert page.index("Lead 6") < page.index("Lead 1")
    assert page.index("Lead 7") < page.index("Lead 1")
    assert 'action="/today/drafts"' in page
    assert 'name="csrf_token"' in page
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
