from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from govscout.companies_house import verified_company_from_profile
from govscout.config import load_settings
from govscout.db import connect_database, insert_verified_lead, migrate
from govscout.sendguard import (
    ReservationConflict,
    ReservationRequest,
    SendGuard,
    SendLimitExceeded,
)


ROOT = Path(__file__).resolve().parents[1]


def _lead(conn, number: int) -> int:
    company = verified_company_from_profile(
        {
            "company_number": f"{number:08d}",
            "company_name": f"Example {number} Ltd",
            "company_status": "active",
            "type": "ltd",
        },
        now=datetime(2026, 7, 20, 9, 0, tzinfo=UTC),
    )
    return insert_verified_lead(
        conn,
        company=company,
        contact_email=f"person{number}@example.test",
        source_register="Test prospect directory",
    )


def _request(lead_id: int, number: int) -> ReservationRequest:
    return ReservationRequest(
        lead_id=lead_id,
        to_email=f"person{number}@example.test",
        stage=0,
        template="signal-led",
        subject="your privacy notice and AI",
        body=f"A compliant test body for lead {number}.",
    )


def test_first_warmup_day_allows_five_reservations_and_blocks_sixth(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    guard = SendGuard(load_settings(ROOT / "config/default.toml"))
    now = datetime(2026, 7, 21, 8, 30, tzinfo=UTC)
    leads = [_lead(conn, number) for number in range(1, 7)]

    accepted = [guard.reserve(conn, _request(lead_id, number), now=now) for number, lead_id in enumerate(leads[:5], 1)]

    assert [reservation.decision.today_count for reservation in accepted] == [1, 2, 3, 4, 5]
    assert accepted[-1].decision.effective_hard_limit == 5
    assert accepted[-1].decision.remaining == 0
    with pytest.raises(SendLimitExceeded) as blocked:
        guard.reserve(conn, _request(leads[5], 6), now=now)
    assert blocked.value.decision.status == "blocked"
    assert blocked.value.decision.today_count == 5
    assert conn.execute(
        "SELECT COUNT(*) FROM sends WHERE state = 'reserved'"
    ).fetchone()[0] == 5


def test_second_warmup_step_allows_eight_and_blocks_ninth(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    guard = SendGuard(load_settings(ROOT / "config/default.toml"))
    conn.execute(
        "INSERT INTO app_state (key, value) VALUES ('warmup_started_at', ?)",
        ("2026-07-01T08:30:00+00:00",),
    )
    now = datetime(2026, 7, 15, 8, 30, tzinfo=UTC)
    leads = [_lead(conn, number) for number in range(1, 10)]

    accepted = [
        guard.reserve(conn, _request(lead_id, number), now=now)
        for number, lead_id in enumerate(leads[:8], 1)
    ]

    assert accepted[-1].decision.warmup_day == 15
    assert accepted[-1].decision.effective_hard_limit == 8
    with pytest.raises(SendLimitExceeded):
        guard.reserve(conn, _request(leads[8], 9), now=now)


def test_full_ramp_warns_at_ten_and_blocks_sixteenth(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    guard = SendGuard(load_settings(ROOT / "config/default.toml"))
    conn.execute(
        "INSERT INTO app_state (key, value) VALUES ('warmup_started_at', ?)",
        ("2026-06-01T08:30:00+00:00",),
    )
    now = datetime(2026, 7, 21, 8, 30, tzinfo=UTC)
    leads = [_lead(conn, number) for number in range(1, 17)]

    reservations = [
        guard.reserve(conn, _request(lead_id, number), now=now)
        for number, lead_id in enumerate(leads[:15], 1)
    ]

    assert reservations[8].decision.status == "ok"
    assert reservations[9].decision.status == "warn"
    assert reservations[9].decision.today_count == 10
    assert reservations[-1].decision.effective_hard_limit == 15
    assert reservations[-1].decision.remaining == 0
    with pytest.raises(SendLimitExceeded):
        guard.reserve(conn, _request(leads[15], 16), now=now)


def test_concurrent_reservations_cannot_race_past_effective_limit(tmp_path):
    database = tmp_path / "govscout.sqlite3"
    setup_conn = connect_database(database)
    migrate(setup_conn)
    leads = [_lead(setup_conn, number) for number in range(1, 13)]
    setup_conn.close()
    guard = SendGuard(load_settings(ROOT / "config/default.toml"))
    now = datetime(2026, 7, 21, 8, 30, tzinfo=UTC)

    def attempt(item):
        number, lead_id = item
        conn = connect_database(database)
        try:
            guard.reserve(conn, _request(lead_id, number), now=now)
            return "accepted"
        except SendLimitExceeded:
            return "blocked"
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(attempt, enumerate(leads, 1)))

    verify_conn = connect_database(database)
    assert results.count("accepted") == 5
    assert results.count("blocked") == 7
    assert verify_conn.execute(
        "SELECT COUNT(*) FROM sends WHERE state = 'reserved'"
    ).fetchone()[0] == 5


def test_same_reservation_is_idempotent_but_changed_content_conflicts(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    guard = SendGuard(load_settings(ROOT / "config/default.toml"))
    now = datetime(2026, 7, 21, 8, 30, tzinfo=UTC)
    request = _request(_lead(conn, 1), 1)

    first = guard.reserve(conn, request, now=now)
    retry = guard.reserve(conn, request, now=now)

    assert retry.send_id == first.send_id
    assert retry.decision.today_count == 1
    with pytest.raises(ReservationConflict):
        guard.reserve(conn, replace(request, subject="changed subject"), now=now)


def test_reservation_recipient_must_match_verified_lead_contact(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    guard = SendGuard(load_settings(ROOT / "config/default.toml"))
    request = replace(
        _request(_lead(conn, 1), 1),
        to_email="unrelated-person@example.test, second-person@example.test",
    )

    with pytest.raises(ReservationConflict, match="verified lead contact"):
        guard.reserve(
            conn,
            request,
            now=datetime(2026, 7, 21, 8, 30, tzinfo=UTC),
        )

    assert conn.execute("SELECT COUNT(*) FROM sends").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM app_state").fetchone()[0] == 0


def test_draft_to_sent_transition_updates_same_counted_ledger_row(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    guard = SendGuard(load_settings(ROOT / "config/default.toml"))
    drafted_at = datetime(2026, 7, 21, 8, 30, tzinfo=UTC)
    reservation = guard.reserve(
        conn, _request(_lead(conn, 1), 1), now=drafted_at
    )
    guard.finalise_draft(
        conn,
        send_id=reservation.send_id,
        draft_id="draft-1",
        message_id="message-1",
        thread_id="thread-1",
        now=drafted_at,
    )

    guard.mark_sent(
        conn,
        send_id=reservation.send_id,
        message_id="message-1",
        thread_id="thread-1",
        now=datetime(2026, 7, 21, 9, 0, tzinfo=UTC),
    )

    row = conn.execute("SELECT id, state, sent_at FROM sends").fetchone()
    assert row[0] == reservation.send_id
    assert row[1] == "sent"
    assert row[2].endswith("+00:00")
    assert guard.status(conn, now=drafted_at).today_count == 1


def test_void_preserves_audit_row_and_releases_daily_capacity(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    guard = SendGuard(load_settings(ROOT / "config/default.toml"))
    now = datetime(2026, 7, 21, 8, 30, tzinfo=UTC)
    reservation = guard.reserve(conn, _request(_lead(conn, 1), 1), now=now)
    guard.finalise_draft(
        conn,
        send_id=reservation.send_id,
        draft_id="draft-1",
        message_id="message-1",
        thread_id="thread-1",
        now=now,
    )

    guard.void_draft(conn, send_id=reservation.send_id, now=now)

    row = conn.execute("SELECT state, voided_at FROM sends").fetchone()
    assert row[0] == "void"
    assert row[1].endswith("+00:00")
    assert guard.status(conn, now=now).today_count == 0


def test_week_count_uses_seven_inclusive_uk_calendar_days(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    guard = SendGuard(load_settings(ROOT / "config/default.toml"))
    guard.reserve(
        conn,
        _request(_lead(conn, 1), 1),
        now=datetime(2026, 7, 15, 8, 30, tzinfo=UTC),
    )
    guard.reserve(
        conn,
        _request(_lead(conn, 2), 2),
        now=datetime(2026, 7, 14, 8, 30, tzinfo=UTC),
    )

    assert guard.week_count(
        conn, datetime(2026, 7, 21, 8, 30, tzinfo=UTC)
    ) == 1


def test_uk_midnight_resets_count_and_advances_warmup_day(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    guard = SendGuard(load_settings(ROOT / "config/default.toml"))
    before_midnight = datetime(2026, 7, 20, 22, 30, tzinfo=UTC)
    guard.reserve(conn, _request(_lead(conn, 1), 1), now=before_midnight)

    after_midnight = datetime(2026, 7, 20, 23, 30, tzinfo=UTC)
    decision = guard.status(conn, now=after_midnight)

    assert decision.today_count == 0
    assert decision.warmup_day == 2
    assert decision.status == "warn"
    assert "Outside the preferred 08:00–11:00 UK drafting window." in decision.messages


def test_uk_day_bounds_cover_the_25_hour_bst_to_gmt_day(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    guard = SendGuard(load_settings(ROOT / "config/default.toml"))
    early_on_october_25 = datetime(2026, 10, 24, 23, 30, tzinfo=UTC)
    guard.reserve(conn, _request(_lead(conn, 1), 1), now=early_on_october_25)

    late_on_october_25 = datetime(2026, 10, 25, 23, 30, tzinfo=UTC)
    next_uk_day = datetime(2026, 10, 26, 0, 30, tzinfo=UTC)

    assert guard.status(conn, now=late_on_october_25).today_count == 1
    assert guard.status(conn, now=next_uk_day).today_count == 0


def test_uk_day_bounds_cover_the_23_hour_gmt_to_bst_day(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    guard = SendGuard(load_settings(ROOT / "config/default.toml"))
    early_on_march_29 = datetime(2026, 3, 29, 0, 30, tzinfo=UTC)
    guard.reserve(conn, _request(_lead(conn, 1), 1), now=early_on_march_29)

    late_on_march_29 = datetime(2026, 3, 29, 22, 30, tzinfo=UTC)
    next_uk_day = datetime(2026, 3, 29, 23, 30, tzinfo=UTC)

    assert guard.status(conn, now=late_on_march_29).today_count == 1
    assert guard.status(conn, now=next_uk_day).today_count == 0
