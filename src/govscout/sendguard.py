from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, time, timedelta
import hashlib
import sqlite3
from typing import Callable
from zoneinfo import ZoneInfo

from govscout.config import Settings
from govscout.email_address import normalise_single_recipient


COUNTABLE_STATES = ("reserved", "draft", "sent")
P1_SENDER_EMAIL = "harrison@misegroup.co.uk"
P1_SENDER_NAME = "Harrison — Mise"
P1_SOFT_LIMIT = 10
P1_HARD_LIMIT = 15
P1_WARMUP = ((14, 5), (28, 8))
P1_WINDOW_UK = (8, 11)
P1_PREFERRED_WEEKDAYS = (2, 3, 4)
P1_FOLLOWUPS_FIRST = True
P1_FOLLOWUP_DAYS = (4, 12, 25)
P1_TIMEZONE = "Europe/London"


@dataclass(frozen=True, slots=True)
class ReservationRequest:
    lead_id: int
    to_email: str
    stage: int
    template: str
    subject: str
    body: str
    sequence_id: int = 1


@dataclass(frozen=True, slots=True)
class GuardDecision:
    status: str
    today_count: int
    soft_limit: int
    configured_hard_limit: int
    effective_hard_limit: int
    remaining: int
    warmup_day: int
    messages: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Reservation:
    send_id: int
    to_email: str
    decision: GuardDecision
    created: bool


class SendLimitExceeded(RuntimeError):
    def __init__(self, decision: GuardDecision):
        super().__init__("daily draft limit reached")
        self.decision = decision


class ReservationConflict(RuntimeError):
    """Raised when a sequence stage is retried with different content or state."""


class SendGuard:
    __slots__ = ()

    def __init__(self, settings: Settings):
        if type(settings) is not Settings:
            raise TypeError("SendGuard requires a validated Settings instance")
        expected = (
            P1_SENDER_EMAIL,
            P1_SENDER_NAME,
            P1_SOFT_LIMIT,
            P1_HARD_LIMIT,
            P1_WARMUP,
            P1_WINDOW_UK,
            P1_PREFERRED_WEEKDAYS,
            P1_FOLLOWUPS_FIRST,
            P1_FOLLOWUP_DAYS,
            P1_TIMEZONE,
        )
        try:
            supplied = (
                settings.sender_email,
                settings.sender_name,
                settings.soft_limit,
                settings.hard_limit,
                settings.warmup,
                settings.window_uk,
                settings.preferred_weekdays,
                settings.followups_first,
                settings.followup_days,
                settings.timezone,
            )
        except AttributeError as exc:
            raise TypeError("SendGuard requires complete validated Settings") from exc
        if supplied != expected:
            raise ValueError("SendGuard settings do not match the fixed P1 contract")

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("SendGuard safety settings are immutable")

    @property
    def sender_email(self) -> str:
        return P1_SENDER_EMAIL

    @property
    def sender_name(self) -> str:
        return P1_SENDER_NAME

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(P1_TIMEZONE)

    @staticmethod
    def _require_aware(now: datetime) -> datetime:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        return now

    @staticmethod
    def _utc_text(value: datetime) -> str:
        return value.astimezone(UTC).isoformat()

    def _uk_day_bounds(self, now: datetime) -> tuple[datetime, datetime]:
        local_now = self._require_aware(now).astimezone(self.timezone)
        start_local = datetime.combine(local_now.date(), time.min, tzinfo=self.timezone)
        end_local = start_local + timedelta(days=1)
        return start_local.astimezone(UTC), end_local.astimezone(UTC)

    def _warmup_started_at(self, conn: sqlite3.Connection) -> datetime | None:
        row = conn.execute(
            "SELECT value FROM app_state WHERE key = 'warmup_started_at'"
        ).fetchone()
        return datetime.fromisoformat(row[0]) if row else None

    def _ensure_warmup_started(self, conn: sqlite3.Connection, now: datetime) -> datetime:
        started_at = self._warmup_started_at(conn)
        if started_at is not None:
            return started_at
        started_at = now.astimezone(UTC)
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES ('warmup_started_at', ?)",
            (self._utc_text(started_at),),
        )
        return started_at

    def _warmup_day(self, conn: sqlite3.Connection, now: datetime) -> int:
        started_at = self._warmup_started_at(conn)
        if started_at is None:
            return 1
        start_day = started_at.astimezone(self.timezone).date()
        current_day = now.astimezone(self.timezone).date()
        return max(1, (current_day - start_day).days + 1)

    def _effective_hard_limit(self, warmup_day: int) -> int:
        for through_day, limit in P1_WARMUP:
            if warmup_day <= through_day:
                return min(P1_HARD_LIMIT, limit)
        return P1_HARD_LIMIT

    def today_count(self, conn: sqlite3.Connection, now: datetime) -> int:
        start, end = self._uk_day_bounds(now)
        placeholders = ", ".join("?" for _ in COUNTABLE_STATES)
        row = conn.execute(
            f"""
            SELECT COUNT(*) FROM sends
            WHERE state IN ({placeholders})
              AND created_at >= ?
              AND created_at < ?
            """,
            (*COUNTABLE_STATES, self._utc_text(start), self._utc_text(end)),
        ).fetchone()
        return int(row[0])

    def week_count(self, conn: sqlite3.Connection, now: datetime) -> int:
        local_now = self._require_aware(now).astimezone(self.timezone)
        start_local = datetime.combine(
            local_now.date() - timedelta(days=6), time.min, tzinfo=self.timezone
        )
        end_local = datetime.combine(
            local_now.date() + timedelta(days=1), time.min, tzinfo=self.timezone
        )
        placeholders = ", ".join("?" for _ in COUNTABLE_STATES)
        row = conn.execute(
            f"""
            SELECT COUNT(*) FROM sends
            WHERE state IN ({placeholders})
              AND created_at >= ?
              AND created_at < ?
            """,
            (
                *COUNTABLE_STATES,
                self._utc_text(start_local),
                self._utc_text(end_local),
            ),
        ).fetchone()
        return int(row[0])

    def _advisories(self, now: datetime) -> tuple[str, ...]:
        local_now = now.astimezone(self.timezone)
        messages: list[str] = []
        if local_now.isoweekday() not in P1_PREFERRED_WEEKDAYS:
            messages.append("Outside the preferred Tuesday–Thursday drafting days.")
        start_hour, end_hour = P1_WINDOW_UK
        if not start_hour <= local_now.hour < end_hour:
            messages.append("Outside the preferred 08:00–11:00 UK drafting window.")
        return tuple(messages)

    def status(self, conn: sqlite3.Connection, *, now: datetime) -> GuardDecision:
        self._require_aware(now)
        count = self.today_count(conn, now)
        warmup_day = self._warmup_day(conn, now)
        effective = self._effective_hard_limit(warmup_day)
        messages = self._advisories(now)
        status = "warn" if messages or count >= P1_SOFT_LIMIT else "ok"
        return GuardDecision(
            status=status,
            today_count=count,
            soft_limit=P1_SOFT_LIMIT,
            configured_hard_limit=P1_HARD_LIMIT,
            effective_hard_limit=effective,
            remaining=max(0, effective - count),
            warmup_day=warmup_day,
            messages=messages,
        )

    def reserve(
        self,
        conn: sqlite3.Connection,
        request: ReservationRequest,
        *,
        now: datetime,
        validator: Callable[[sqlite3.Connection], None] | None = None,
    ) -> Reservation:
        self._require_aware(now)
        try:
            to_email = normalise_single_recipient(request.to_email)
        except ValueError as exc:
            raise ReservationConflict(
                "draft recipient must be one valid address"
            ) from exc
        try:
            conn.execute("BEGIN IMMEDIATE")
            if validator is not None:
                validator(conn)
            lead = conn.execute(
                "SELECT contact_email FROM leads WHERE id = ?",
                (request.lead_id,),
            ).fetchone()
            if lead is None or lead["contact_email"] != to_email:
                raise ReservationConflict(
                    "draft recipient does not match the verified lead contact"
                )
            template = request.template.strip()
            subject = request.subject.strip()
            body_hash = hashlib.sha256(request.body.encode("utf-8")).hexdigest()
            word_count = len(request.body.split())
            existing = conn.execute(
                """
                SELECT id, to_email, template, subject, body_hash, word_count, state
                FROM sends
                WHERE lead_id = ? AND sequence_id = ? AND stage = ?
                """,
                (request.lead_id, request.sequence_id, request.stage),
            ).fetchone()
            if existing is not None:
                same_request = (
                    existing["to_email"] == to_email
                    and existing["template"] == template
                    and existing["subject"] == subject
                    and existing["body_hash"] == body_hash
                    and existing["word_count"] == word_count
                )
                if same_request and existing["state"] in COUNTABLE_STATES:
                    decision = self.status(conn, now=now)
                    conn.execute("COMMIT")
                    return Reservation(
                        send_id=existing["id"],
                        to_email=existing["to_email"],
                        decision=decision,
                        created=False,
                    )
                conn.execute("ROLLBACK")
                raise ReservationConflict(
                    "sequence stage already exists with different content or final state"
                )

            self._ensure_warmup_started(conn, now)
            before = self.status(conn, now=now)
            if before.today_count + 1 > before.effective_hard_limit:
                blocked = replace(
                    before,
                    status="blocked",
                    messages=before.messages + ("Effective daily hard limit reached.",),
                )
                conn.execute("ROLLBACK")
                raise SendLimitExceeded(blocked)

            cursor = conn.execute(
                """
                INSERT INTO sends (
                    lead_id, sequence_id, to_email, stage, template, subject,
                    body_hash, word_count, state, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?)
                """,
                (
                    request.lead_id,
                    request.sequence_id,
                    to_email,
                    request.stage,
                    template,
                    subject,
                    body_hash,
                    word_count,
                    self._utc_text(now),
                ),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("SQLite did not return a send id")
            after = self.status(conn, now=now)
            conn.execute("COMMIT")
            return Reservation(
                send_id=cursor.lastrowid,
                to_email=to_email,
                decision=after,
                created=True,
            )
        except SendLimitExceeded:
            raise
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def note_reservation_error(
        self,
        conn: sqlite3.Connection,
        *,
        send_id: int,
        reason: str,
    ) -> None:
        cursor = conn.execute(
            """
            UPDATE sends
            SET failure_reason = ?
            WHERE id = ? AND state = 'reserved'
            """,
            (reason[:500], send_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("send reservation is missing or is not reservable")

    def finalise_draft(
        self,
        conn: sqlite3.Connection,
        *,
        send_id: int,
        draft_id: str,
        message_id: str | None,
        thread_id: str | None,
        now: datetime,
    ) -> None:
        self._require_aware(now)
        cursor = conn.execute(
            """
            UPDATE sends
            SET state = 'draft', drafted_at = ?, gmail_draft_id = ?,
                gmail_message_id = ?, gmail_thread_id = ?, failure_reason = NULL
            WHERE id = ? AND state = 'reserved'
            """,
            (
                self._utc_text(now),
                draft_id,
                message_id,
                thread_id,
                send_id,
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError("send reservation is missing or is not reservable")

    def mark_sent(
        self,
        conn: sqlite3.Connection,
        *,
        send_id: int,
        message_id: str,
        thread_id: str | None,
        now: datetime,
    ) -> None:
        self._require_aware(now)
        cursor = conn.execute(
            """
            UPDATE sends
            SET state = 'sent', sent_at = ?, gmail_message_id = ?,
                gmail_thread_id = ?, failure_reason = NULL
            WHERE id = ? AND state = 'draft'
            """,
            (self._utc_text(now), message_id, thread_id, send_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("send ledger row is missing or is not a draft")

    def void_draft(
        self,
        conn: sqlite3.Connection,
        *,
        send_id: int,
        now: datetime,
    ) -> None:
        self._require_aware(now)
        cursor = conn.execute(
            """
            UPDATE sends
            SET state = 'void', voided_at = ?, failure_reason = NULL
            WHERE id = ? AND state = 'draft'
            """,
            (self._utc_text(now), send_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("send ledger row is missing or is not a draft")
