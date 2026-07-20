from datetime import UTC, datetime
from pathlib import Path

from govscout.cli import main
from govscout.companies_house import verified_company_from_profile
from govscout.config import load_settings
from govscout.db import connect_database, insert_verified_lead, migrate
from govscout.draft_service import DraftService
from govscout.policy import PolicyResult
from govscout.sendguard import ReservationRequest, SendGuard


ROOT = Path(__file__).resolve().parents[1]


class AllowPolicy:
    def evaluate(self, request):
        return PolicyResult(passed=True)


class FakeGmail:
    def __init__(self):
        self.created = []
        self.deleted = []

    def find_by_ledger_id(self, ledger_id):
        return None

    def create_draft(self, **kwargs):
        self.created.append(kwargs)
        return {
            "draft_id": "draft-cli",
            "message_id": "message-cli",
            "thread_id": "thread-cli",
        }

    def delete_draft(self, draft_id):
        self.deleted.append(draft_id)


class StaticCandidates:
    def __init__(self, requests):
        self.requests = requests

    def get(self, lead_id):
        return next(item for item in self.requests if item.lead_id == lead_id)

    def due(self):
        return list(self.requests)


def _dependencies(tmp_path):
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
        body="Compliant test copy.",
    )
    guard = SendGuard(load_settings(ROOT / "config/default.toml"))
    gmail = FakeGmail()
    service = DraftService(guard=guard, policy=AllowPolicy(), gmail=gmail)
    return conn, guard, gmail, service, StaticCandidates([request]), lead_id


def test_cli_draft_and_undo_route_only_through_draft_service(tmp_path, capsys):
    conn, guard, gmail, service, candidates, lead_id = _dependencies(tmp_path)
    now = datetime(2026, 7, 21, 8, 30, tzinfo=UTC)

    draft_exit = main(
        ["draft", str(lead_id)],
        conn=conn,
        guard=guard,
        draft_service=service,
        candidate_source=candidates,
        now=now,
    )
    send_id = conn.execute("SELECT id FROM sends").fetchone()[0]
    undo_exit = main(
        ["send-undo", str(send_id)],
        conn=conn,
        guard=guard,
        draft_service=service,
        now=now,
    )

    assert draft_exit == 0
    assert undo_exit == 0
    assert len(gmail.created) == 1
    assert gmail.deleted == ["draft-cli"]
    assert conn.execute("SELECT state FROM sends").fetchone()[0] == "void"
    output = capsys.readouterr().out
    assert "Draft created" in output
    assert "Draft voided" in output


def test_cli_draft_commands_are_fail_closed_without_wired_service(tmp_path, capsys):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    guard = SendGuard(load_settings(ROOT / "config/default.toml"))

    exit_code = main(
        ["draft", "1"],
        conn=conn,
        guard=guard,
        now=datetime(2026, 7, 21, 8, 30, tzinfo=UTC),
    )

    assert exit_code == 2
    assert "LINT_NOT_READY" in capsys.readouterr().out
    assert conn.execute("SELECT COUNT(*) FROM sends").fetchone()[0] == 0


def test_cli_batch_retry_reports_existing_draft_without_counting_it_as_new(
    tmp_path,
    capsys,
):
    conn, guard, gmail, service, candidates, _ = _dependencies(tmp_path)
    now = datetime(2026, 7, 21, 8, 30, tzinfo=UTC)

    first_exit = main(
        ["draft-batch"],
        conn=conn,
        guard=guard,
        draft_service=service,
        candidate_source=candidates,
        now=now,
    )
    retry_exit = main(
        ["draft-batch"],
        conn=conn,
        guard=guard,
        draft_service=service,
        candidate_source=candidates,
        now=now,
    )

    assert first_exit == 0
    assert retry_exit == 0
    assert len(gmail.created) == 1
    output = capsys.readouterr().out
    assert "Batch drafted 1; rolled over 0" in output
    assert "Batch drafted 0; rolled over 0" in output
