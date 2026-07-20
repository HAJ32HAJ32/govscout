from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import sqlite3
from typing import Mapping, Protocol

from govscout.policy import DraftPolicy
from govscout.sendguard import GuardDecision, ReservationRequest, SendGuard


class GmailDraftPort(Protocol):
    def find_by_ledger_id(self, ledger_id: int) -> Mapping[str, str | None] | None: ...

    def create_draft(self, **kwargs: object) -> Mapping[str, str | None]: ...

    def delete_draft(self, draft_id: str) -> None: ...


class DraftPolicyRefused(RuntimeError):
    def __init__(self, reasons: tuple[str, ...]):
        self.reasons = reasons
        super().__init__(", ".join(reasons))


class DraftOutcomeUncertain(RuntimeError):
    """Raised when an earlier Gmail call cannot yet be reconciled safely."""


class DraftAlreadySent(RuntimeError):
    """Raised when a draft retry targets a ledger row already marked sent."""


@dataclass(frozen=True, slots=True)
class DraftResult:
    send_id: int
    draft_id: str
    message_id: str | None
    thread_id: str | None
    decision: GuardDecision
    created: bool


class DraftService:
    def __init__(self, *, guard: SendGuard, policy: DraftPolicy, gmail: GmailDraftPort):
        self.guard = guard
        self.policy = policy
        self.gmail = gmail

    @staticmethod
    def _gmail_fields(
        gmail_result: Mapping[str, str | None],
    ) -> tuple[str, str | None, str | None]:
        draft_id = gmail_result.get("draft_id")
        if not draft_id:
            raise RuntimeError("Gmail did not return a draft id")
        return draft_id, gmail_result.get("message_id"), gmail_result.get("thread_id")

    def _resume_existing(
        self,
        conn: sqlite3.Connection,
        *,
        send_id: int,
        decision: GuardDecision,
        now: datetime,
    ) -> DraftResult:
        row = conn.execute(
            """
            SELECT state, gmail_draft_id, gmail_message_id, gmail_thread_id
            FROM sends WHERE id = ?
            """,
            (send_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("reserved send ledger row disappeared")
        if row["state"] == "draft":
            if not row["gmail_draft_id"]:
                raise RuntimeError("draft ledger row is missing its Gmail draft id")
            return DraftResult(
                send_id=send_id,
                draft_id=row["gmail_draft_id"],
                message_id=row["gmail_message_id"],
                thread_id=row["gmail_thread_id"],
                decision=decision,
                created=False,
            )
        if row["state"] == "sent":
            raise DraftAlreadySent("ledger row has already been sent manually")

        recovered = self.gmail.find_by_ledger_id(send_id)
        if recovered is None:
            raise DraftOutcomeUncertain(
                "existing reservation could not be reconciled; no duplicate was created"
            )
        draft_id, message_id, thread_id = self._gmail_fields(recovered)
        self.guard.finalise_draft(
            conn,
            send_id=send_id,
            draft_id=draft_id,
            message_id=message_id,
            thread_id=thread_id,
            now=now,
        )
        return DraftResult(
            send_id=send_id,
            draft_id=draft_id,
            message_id=message_id,
            thread_id=thread_id,
            decision=decision,
            created=False,
        )

    def undo_draft(
        self,
        conn: sqlite3.Connection,
        *,
        send_id: int,
        now: datetime,
    ) -> None:
        row = conn.execute(
            "SELECT state, gmail_draft_id FROM sends WHERE id = ?",
            (send_id,),
        ).fetchone()
        if row is None or row["state"] != "draft" or not row["gmail_draft_id"]:
            raise ValueError("send ledger row is missing or is not an undoable draft")
        self.gmail.delete_draft(row["gmail_draft_id"])
        self.guard.void_draft(conn, send_id=send_id, now=now)

    def create_review_draft(
        self,
        conn: sqlite3.Connection,
        request: ReservationRequest,
        *,
        now: datetime,
    ) -> DraftResult:
        policy_result = self.policy.evaluate(request)
        if not policy_result.passed:
            raise DraftPolicyRefused(policy_result.reasons)

        reservation = self.guard.reserve(conn, request, now=now)
        if not reservation.created:
            return self._resume_existing(
                conn,
                send_id=reservation.send_id,
                decision=reservation.decision,
                now=now,
            )

        try:
            gmail_result = self.gmail.create_draft(
                to_email=reservation.to_email,
                from_email=self.guard.sender_email,
                from_name=self.guard.sender_name,
                subject=request.subject,
                body=request.body,
                ledger_id=reservation.send_id,
            )
            draft_id, message_id, thread_id = self._gmail_fields(gmail_result)
        except Exception as exc:
            self.guard.note_reservation_error(
                conn,
                send_id=reservation.send_id,
                reason=f"Gmail draft outcome uncertain ({type(exc).__name__})",
            )
            raise
        self.guard.finalise_draft(
            conn,
            send_id=reservation.send_id,
            draft_id=draft_id,
            message_id=message_id,
            thread_id=thread_id,
            now=now,
        )
        return DraftResult(
            send_id=reservation.send_id,
            draft_id=draft_id,
            message_id=message_id,
            thread_id=thread_id,
            decision=reservation.decision,
            created=True,
        )
