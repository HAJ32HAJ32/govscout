from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_MAXMEM = 64 * 1024 * 1024
PUBLIC_HOST = "leads.misegroup.co.uk"


@dataclass(frozen=True, slots=True)
class AuthConfig:
    username: str
    password_hash: str
    session_secret: bytes
    public_host: str = PUBLIC_HOST
    public_https: bool = True
    max_failures: int = 5

    def __post_init__(self) -> None:
        if not self.username or self.username != self.username.strip():
            raise ValueError("authentication username must be non-empty and trimmed")
        if not _valid_hash_shape(self.password_hash):
            raise ValueError("authentication password hash is invalid")
        if not isinstance(self.session_secret, bytes) or len(self.session_secret) < 32:
            raise ValueError("session secret must contain at least 32 bytes")
        if self.public_host != PUBLIC_HOST:
            raise ValueError(f"public host must be exactly {PUBLIC_HOST}")
        if self.public_https is not True:
            raise ValueError("public proxy mode requires HTTPS")
        if self.max_failures < 1:
            raise ValueError("maximum login failures must be positive")


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    if not value or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for character in value):
        raise ValueError("invalid base64url")
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)


def _valid_hash_shape(encoded: str) -> bool:
    if not isinstance(encoded, str):
        return False
    try:
        algorithm, version, n_text, r_text, p_text, salt_text, digest_text = encoded.split("$")
        salt = _b64decode(salt_text)
        digest = _b64decode(digest_text)
    except (ValueError, TypeError):
        return False
    return (
        algorithm == "scrypt"
        and version == "v=1"
        and n_text == f"n={SCRYPT_N}"
        and r_text == f"r={SCRYPT_R}"
        and p_text == f"p={SCRYPT_P}"
        and len(salt) >= 16
        and len(digest) == SCRYPT_DKLEN
    )


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    if not isinstance(password, str) or not password:
        raise ValueError("password must be a non-empty string")
    actual_salt = salt or secrets.token_bytes(16)
    if len(actual_salt) < 16:
        raise ValueError("salt must contain at least 16 bytes")
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=actual_salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
        maxmem=SCRYPT_MAXMEM,
    )
    return (
        f"scrypt$v=1$n={SCRYPT_N}$r={SCRYPT_R}$p={SCRYPT_P}$"
        f"{_b64encode(actual_salt)}${_b64encode(digest)}"
    )


def verify_password(password: str, encoded: str) -> bool:
    if not isinstance(password, str) or not isinstance(encoded, str):
        return False
    try:
        algorithm, version, n_text, r_text, p_text, salt_text, digest_text = encoded.split("$")
        if (
            algorithm != "scrypt"
            or version != "v=1"
            or n_text != f"n={SCRYPT_N}"
            or r_text != f"r={SCRYPT_R}"
            or p_text != f"p={SCRYPT_P}"
        ):
            return False
        salt = _b64decode(salt_text)
        expected = _b64decode(digest_text)
        if len(salt) < 16 or len(expected) != SCRYPT_DKLEN:
            return False
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=SCRYPT_N,
            r=SCRYPT_R,
            p=SCRYPT_P,
            dklen=SCRYPT_DKLEN,
            maxmem=SCRYPT_MAXMEM,
        )
    except (ValueError, TypeError, UnicodeError):
        return False
    return hmac.compare_digest(actual, expected)


@dataclass(frozen=True, slots=True)
class LoginAdmission:
    admitted: bool
    blocked_after: bool
    token: str | None = None


@dataclass(frozen=True, slots=True)
class LoginThrottle:
    max_failures: int = 5
    window: timedelta = timedelta(minutes=15)
    lockout: timedelta = timedelta(minutes=15)
    bucket: str = "single-user"

    def __post_init__(self) -> None:
        if self.max_failures < 1 or self.window <= timedelta(0) or self.lockout <= timedelta(0):
            raise ValueError("login throttle limits must be positive")

    @staticmethod
    def _utc(now: datetime) -> datetime:
        if now.tzinfo is None:
            raise ValueError("login throttle time must be timezone-aware")
        return now.astimezone(UTC)

    def _persist_failure(self, conn: sqlite3.Connection, *, instant: datetime) -> bool:
        row = conn.execute(
            "SELECT failure_count, window_started_at, blocked_until FROM login_throttle WHERE bucket = ?",
            (self.bucket,),
        ).fetchone()
        if row is None:
            count = 1
            window_started = instant
        else:
            prior_start = datetime.fromisoformat(row["window_started_at"])
            prior_blocked = datetime.fromisoformat(row["blocked_until"]) if row["blocked_until"] else None
            expired = instant - prior_start >= self.window or (
                prior_blocked is not None and instant >= prior_blocked
            )
            count = 1 if expired else row["failure_count"] + 1
            window_started = instant if expired else prior_start
        blocked_until = instant + self.lockout if count >= self.max_failures else None
        conn.execute(
            """
            INSERT INTO login_throttle (
                bucket, failure_count, window_started_at, blocked_until, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(bucket) DO UPDATE SET
                failure_count = excluded.failure_count,
                window_started_at = excluded.window_started_at,
                blocked_until = excluded.blocked_until,
                updated_at = excluded.updated_at
            """,
            (
                self.bucket,
                count,
                window_started.isoformat(),
                blocked_until.isoformat() if blocked_until else None,
                instant.isoformat(),
            ),
        )
        return blocked_until is not None

    def _convert_expired_reservations(self, conn: sqlite3.Connection, *, instant: datetime) -> None:
        expired = conn.execute(
            """
            SELECT token, expires_at FROM login_attempt_reservations
            WHERE bucket = ? AND expires_at <= ?
            ORDER BY expires_at, token
            """,
            (self.bucket, instant.isoformat()),
        ).fetchall()
        for reservation in expired:
            deleted = conn.execute(
                "DELETE FROM login_attempt_reservations WHERE token = ? AND bucket = ?",
                (reservation["token"], self.bucket),
            )
            if deleted.rowcount:
                self._persist_failure(
                    conn,
                    instant=datetime.fromisoformat(reservation["expires_at"]),
                )

    def is_blocked(self, conn: sqlite3.Connection, *, now: datetime) -> bool:
        instant = self._utc(now)
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._convert_expired_reservations(conn, instant=instant)
            row = conn.execute(
                "SELECT failure_count, window_started_at, blocked_until FROM login_throttle WHERE bucket = ?",
                (self.bucket,),
            ).fetchone()
            blocked = False
            if row is not None:
                blocked_until = datetime.fromisoformat(row["blocked_until"]) if row["blocked_until"] else None
                window_started = datetime.fromisoformat(row["window_started_at"])
                blocked = blocked_until is not None and instant < blocked_until
                if not blocked and (blocked_until is not None or instant - window_started >= self.window):
                    conn.execute(
                        """
                        UPDATE login_throttle
                        SET failure_count = 0, window_started_at = ?, blocked_until = NULL, updated_at = ?
                        WHERE bucket = ?
                        """,
                        (instant.isoformat(), instant.isoformat(), self.bucket),
                    )
            failure_row = conn.execute(
                "SELECT failure_count FROM login_throttle WHERE bucket = ?",
                (self.bucket,),
            ).fetchone()
            failure_count = failure_row["failure_count"] if failure_row is not None else 0
            active = conn.execute(
                "SELECT count(*) FROM login_attempt_reservations WHERE bucket = ?",
                (self.bucket,),
            ).fetchone()[0]
            blocked = blocked or failure_count + active >= self.max_failures
            conn.execute("COMMIT")
            return blocked
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def reserve_attempt(self, conn: sqlite3.Connection, *, now: datetime) -> LoginAdmission:
        instant = self._utc(now)
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._convert_expired_reservations(conn, instant=instant)
            row = conn.execute(
                "SELECT failure_count, window_started_at, blocked_until FROM login_throttle WHERE bucket = ?",
                (self.bucket,),
            ).fetchone()
            failure_count = 0
            if row is not None:
                prior_start = datetime.fromisoformat(row["window_started_at"])
                prior_blocked = datetime.fromisoformat(row["blocked_until"]) if row["blocked_until"] else None
                if prior_blocked is not None and instant < prior_blocked:
                    conn.execute("COMMIT")
                    return LoginAdmission(admitted=False, blocked_after=True)
                expired = instant - prior_start >= self.window or prior_blocked is not None
                failure_count = 0 if expired else row["failure_count"]
                if expired:
                    conn.execute("DELETE FROM login_throttle WHERE bucket = ?", (self.bucket,))
            active = conn.execute(
                "SELECT count(*) FROM login_attempt_reservations WHERE bucket = ?",
                (self.bucket,),
            ).fetchone()[0]
            if failure_count + active >= self.max_failures:
                conn.execute("COMMIT")
                return LoginAdmission(admitted=False, blocked_after=True)
            token = secrets.token_urlsafe(32)
            conn.execute(
                """
                INSERT INTO login_attempt_reservations (token, bucket, reserved_at, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    token,
                    self.bucket,
                    instant.isoformat(),
                    (instant + self.window).isoformat(),
                ),
            )
            conn.execute("COMMIT")
            return LoginAdmission(
                admitted=True,
                blocked_after=failure_count + active + 1 >= self.max_failures,
                token=token,
            )
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def finalize_failure(self, conn: sqlite3.Connection, *, token: str, now: datetime) -> bool:
        instant = self._utc(now)
        try:
            conn.execute("BEGIN IMMEDIATE")
            deleted = conn.execute(
                "DELETE FROM login_attempt_reservations WHERE token = ? AND bucket = ?",
                (token, self.bucket),
            )
            if not deleted.rowcount:
                conn.execute("COMMIT")
                return self.is_blocked(conn, now=instant)
            blocked = self._persist_failure(conn, instant=instant)
            conn.execute("COMMIT")
            return blocked
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def finalize_success(self, conn: sqlite3.Connection, *, token: str) -> None:
        try:
            conn.execute("BEGIN IMMEDIATE")
            deleted = conn.execute(
                "DELETE FROM login_attempt_reservations WHERE token = ? AND bucket = ?",
                (token, self.bucket),
            )
            if deleted.rowcount:
                conn.execute("DELETE FROM login_throttle WHERE bucket = ?", (self.bucket,))
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
