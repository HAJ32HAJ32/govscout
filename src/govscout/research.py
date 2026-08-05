from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import sqlite3


class ResearchConflict(RuntimeError):
    """The operator acted on stale research state."""


@dataclass(frozen=True, slots=True)
class ArchiveState:
    event_id: int | None
    archived: bool
    reason: str | None


def latest_archive_state(
    conn: sqlite3.Connection,
    *,
    firm_id: int,
) -> ArchiveState:
    event = conn.execute(
        """
        SELECT id, action, reason
        FROM firm_archive_events
        WHERE firm_id = ?
        ORDER BY id DESC LIMIT 1
        """,
        (firm_id,),
    ).fetchone()
    if event is None:
        return ArchiveState(None, False, None)
    return ArchiveState(
        int(event["id"]),
        event["action"] == "archive",
        event["reason"],
    )


def record_archive_event(
    conn: sqlite3.Connection,
    *,
    firm_id: int,
    action: str,
    reason: str | None,
    actor: str,
    expected_previous_event_id: int | None,
    now: datetime,
) -> int:
    if action not in {"archive", "restore"}:
        raise ValueError("research action must be archive or restore")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    clean_reason = reason.strip() if reason and reason.strip() else None
    if clean_reason is None:
        raise ValueError(f"{action} requires a reason")
    if len(clean_reason) > 500:
        raise ValueError("archive reason must be at most 500 characters")
    clean_actor = actor.strip()
    if not 1 <= len(clean_actor) <= 100:
        raise ValueError("archive actor must be between 1 and 100 characters")
    if expected_previous_event_id is not None and expected_previous_event_id <= 0:
        raise ValueError("expected archive event id must be positive")
    if conn.in_transaction:
        raise sqlite3.OperationalError("archive action requires no active transaction")
    try:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM fca_firms WHERE id = ?", (firm_id,)).fetchone() is None:
            raise KeyError(firm_id)
        current = latest_archive_state(conn, firm_id=firm_id)
        if current.event_id != expected_previous_event_id:
            raise ResearchConflict("archive state changed; refresh and try again")
        if action == "archive" and current.archived:
            raise ValueError("firm is already archived")
        if action == "restore" and not current.archived:
            raise ValueError("firm is not archived")
        running = conn.execute(
            """
            SELECT 1 FROM fca_processing_jobs
            WHERE firm_id = ? AND state = 'running'
            LIMIT 1
            """,
            (firm_id,),
        ).fetchone()
        if running is not None:
            raise ResearchConflict("firm processing is currently running")
        event_id = conn.execute(
            """
            INSERT INTO firm_archive_events (
                firm_id, action, reason, actor, expected_previous_event_id, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                firm_id,
                action,
                clean_reason,
                clean_actor,
                expected_previous_event_id,
                now.astimezone(UTC).isoformat(),
            ),
        ).lastrowid
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    if event_id is None:
        raise RuntimeError("SQLite did not return an archive event id")
    return int(event_id)
