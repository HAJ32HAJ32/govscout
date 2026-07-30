from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from threading import Barrier, Thread

import pytest

from govscout.auth import (
    LoginThrottle,
    authenticate_collector_token,
    create_collector_device,
    hash_password,
    revoke_collector_device,
    verify_password,
)
from govscout.db import connect_database, migrate


def test_versioned_scrypt_hash_verifies_only_the_right_password():
    encoded = hash_password("correct horse battery staple", salt=b"0123456789abcdef")

    assert encoded.startswith("scrypt$v=1$")
    assert "correct horse battery staple" not in encoded
    assert verify_password("correct horse battery staple", encoded) is True
    assert verify_password("wrong", encoded) is False


@pytest.mark.parametrize(
    "encoded",
    [
        "",
        "not-a-hash",
        "scrypt$v=2$n=32768$r=8$p=1$MDEyMzQ1Njc4OWFiY2RlZg$bad",
        "scrypt$v=1$n=nope$r=8$p=1$salt$digest",
        "scrypt$v=1$n=1$r=8$p=1$salt$digest",
        "scrypt$v=1$n=32768$r=8$p=1$%%%$%%%",
    ],
)
def test_malformed_or_unsupported_password_hashes_fail_closed(encoded):
    assert verify_password("anything", encoded) is False


def test_collector_device_token_is_one_purpose_hashed_and_revocable(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    issued_at = datetime(2026, 7, 30, 10, tzinfo=UTC)

    credential = create_collector_device(
        conn,
        display_name="H Windows PC",
        now=issued_at,
    )

    assert credential.token.startswith(f"gsc_{credential.device_id}_")
    stored = conn.execute(
        "SELECT display_name, token_hash, last_used_at, revoked_at FROM collector_devices"
    ).fetchone()
    assert stored["display_name"] == "H Windows PC"
    assert credential.token not in tuple(stored)
    assert len(stored["token_hash"]) == 64
    assert conn.execute("SELECT scope FROM collector_devices").fetchone()[0] == "fca_upload"
    assert authenticate_collector_token(
        conn,
        credential.token,
        now=issued_at + timedelta(minutes=1),
    ) == credential.device_id
    assert authenticate_collector_token(
        conn,
        credential.token + "x",
        now=issued_at + timedelta(minutes=2),
    ) is None

    revoke_collector_device(
        conn,
        device_id=credential.device_id,
        now=issued_at + timedelta(minutes=3),
    )

    assert authenticate_collector_token(
        conn,
        credential.token,
        now=issued_at + timedelta(minutes=4),
    ) is None

    with pytest.raises(sqlite3.IntegrityError, match="revocation is immutable"):
        conn.execute(
            "UPDATE collector_devices SET revoked_at = NULL WHERE device_id = ?",
            (credential.device_id,),
        )


def test_sqlite_throttle_blocks_at_limit_and_recovers_after_expiry(tmp_path):
    database = tmp_path / "govscout.sqlite3"
    conn = connect_database(database)
    migrate(conn)
    throttle = LoginThrottle(max_failures=3, window=timedelta(minutes=10), lockout=timedelta(minutes=15))
    now = datetime(2026, 7, 29, 9, tzinfo=UTC)

    assert throttle.is_blocked(conn, now=now) is False
    for offset, blocked_after in [(0, False), (1, False), (2, True)]:
        instant = now + timedelta(seconds=offset)
        admission = throttle.reserve_attempt(conn, now=instant)
        assert admission.token is not None
        assert throttle.finalize_failure(conn, token=admission.token, now=instant) is blocked_after
    assert throttle.is_blocked(conn, now=now + timedelta(minutes=14)) is True
    assert throttle.is_blocked(conn, now=now + timedelta(minutes=16)) is False

    row = conn.execute("SELECT bucket, failure_count FROM login_throttle").fetchone()
    assert tuple(row) == ("single-user", 0)


def test_sqlite_throttle_atomically_reserves_only_one_concurrent_admission(tmp_path):
    database = tmp_path / "govscout.sqlite3"
    setup = connect_database(database)
    migrate(setup)
    setup.close()
    throttle = LoginThrottle(max_failures=1)
    barrier = Barrier(2)
    admissions = []
    errors = []

    def reserve_from_worker() -> None:
        try:
            conn = connect_database(database)
            barrier.wait()
            admissions.append(
                throttle.reserve_attempt(
                    conn,
                    now=datetime(2026, 7, 29, 10, tzinfo=UTC),
                )
            )
            conn.close()
        except Exception as exc:  # noqa: BLE001  # pragma: no cover - asserted below
            errors.append(exc)

    workers = [Thread(target=reserve_from_worker) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert errors == []
    assert sorted(admission.admitted for admission in admissions) == [False, True]
    verify = connect_database(database)
    assert verify.execute("SELECT count(*) FROM login_attempt_reservations").fetchone()[0] == 1
    assert verify.execute("SELECT count(*) FROM login_throttle").fetchone()[0] == 0
    assert throttle.is_blocked(
        verify,
        now=datetime(2026, 7, 29, 10, tzinfo=UTC),
    ) is True


def test_sqlite_throttle_serializes_failures_from_multiple_worker_connections(tmp_path):
    database = tmp_path / "govscout.sqlite3"
    setup = connect_database(database)
    migrate(setup)
    setup.close()
    throttle = LoginThrottle(max_failures=100)
    barrier = Barrier(8)
    errors = []

    def fail_from_worker(worker: int) -> None:
        try:
            conn = connect_database(database)
            barrier.wait()
            instant = datetime(2026, 7, 29, 10, 0, worker, tzinfo=UTC)
            admission = throttle.reserve_attempt(conn, now=instant)
            assert admission.token is not None
            throttle.finalize_failure(conn, token=admission.token, now=instant)
            conn.close()
        except Exception as exc:  # noqa: BLE001  # pragma: no cover - asserted below
            errors.append(exc)

    workers = [Thread(target=fail_from_worker, args=(worker,)) for worker in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert errors == []
    verify = connect_database(database)
    assert verify.execute("SELECT failure_count FROM login_throttle").fetchone()[0] == 8


def test_success_clears_persisted_throttle_state(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    throttle = LoginThrottle()
    now = datetime(2026, 7, 29, 10, tzinfo=UTC)
    failure = throttle.reserve_attempt(conn, now=now)
    assert failure.token is not None
    throttle.finalize_failure(conn, token=failure.token, now=now)
    success = throttle.reserve_attempt(conn, now=now + timedelta(seconds=1))
    assert success.token is not None

    throttle.finalize_success(conn, token=success.token)

    assert conn.execute("SELECT count(*) FROM login_throttle").fetchone()[0] == 0


def test_late_failure_survives_concurrent_success(tmp_path):
    database = tmp_path / "govscout.sqlite3"
    first = connect_database(database)
    migrate(first)
    second = connect_database(database)
    throttle = LoginThrottle(max_failures=5)
    now = datetime(2026, 7, 29, 10, tzinfo=UTC)

    good = throttle.reserve_attempt(first, now=now)
    bad = throttle.reserve_attempt(second, now=now)
    assert good.token is not None
    assert bad.token is not None

    throttle.finalize_success(first, token=good.token)
    throttle.finalize_failure(second, token=bad.token, now=now + timedelta(seconds=1))

    row = first.execute(
        "SELECT failure_count, blocked_until FROM login_throttle WHERE bucket = 'single-user'"
    ).fetchone()
    assert tuple(row) == (1, None)
    assert first.execute("SELECT count(*) FROM login_attempt_reservations").fetchone()[0] == 0


def test_expired_worker_reservation_becomes_a_persisted_failure(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    throttle = LoginThrottle(
        max_failures=1,
        window=timedelta(minutes=10),
        lockout=timedelta(minutes=15),
    )
    now = datetime(2026, 7, 29, 10, tzinfo=UTC)

    abandoned = throttle.reserve_attempt(conn, now=now)
    assert abandoned.admitted is True
    assert throttle.reserve_attempt(conn, now=now + timedelta(minutes=9)).admitted is False

    after_expiry = throttle.reserve_attempt(conn, now=now + timedelta(minutes=11))

    assert after_expiry.admitted is False
    assert conn.execute("SELECT count(*) FROM login_attempt_reservations").fetchone()[0] == 0
    row = conn.execute("SELECT failure_count, blocked_until FROM login_throttle").fetchone()
    assert row[0] == 1
    assert datetime.fromisoformat(row[1]) > now + timedelta(minutes=11)
