from __future__ import annotations

from datetime import UTC, datetime
import sqlite3

from govscout.website_research import WebsiteResearchConflict, _canonical_url, _printable_ascii


def _optional_text(value: object, *, field: str, minimum: int, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    return _printable_ascii(value, field=field, minimum=minimum, maximum=maximum)


def _validate_email(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    email = _printable_ascii(value, field="contact email", minimum=3, maximum=254)
    if "@" not in email[1:]:
        raise ValueError("contact email must contain @")
    return email


def record_contact_evidence(
    conn: sqlite3.Connection,
    *,
    firm_id: int,
    action: str,
    email: str | None,
    phone: str | None,
    contact_name: str | None,
    evidence_url: str,
    justification: str,
    actor: str,
    expected_previous_event_id: int | None,
    now: datetime,
) -> int:
    if action not in {"assert", "withdraw"}:
        raise ValueError("contact evidence action must be assert or withdraw")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if action == "withdraw":
        clean_email = clean_phone = clean_contact_name = None
    else:
        clean_email = _validate_email(email)
        clean_phone = _optional_text(phone, field="contact phone", minimum=3, maximum=40)
        clean_contact_name = _optional_text(
            contact_name, field="contact name", minimum=1, maximum=200
        )
        if clean_email is None and clean_phone is None and clean_contact_name is None:
            raise ValueError("at least one of email, phone or contact name is required")
    evidence = _canonical_url(evidence_url, field="evidence URL", allow_query=True)
    reason = _printable_ascii(
        justification, field="contact evidence justification", minimum=10, maximum=1000
    )
    clean_actor = _printable_ascii(actor, field="contact evidence actor", minimum=1, maximum=100)
    if expected_previous_event_id is not None and expected_previous_event_id <= 0:
        raise ValueError("expected contact evidence event id must be positive")
    owns_transaction = not conn.in_transaction
    try:
        if owns_transaction:
            conn.execute("BEGIN IMMEDIATE")
        firm = conn.execute(
            "SELECT source_record_hash FROM fca_firms WHERE id = ?", (firm_id,)
        ).fetchone()
        if firm is None:
            raise KeyError(firm_id)
        source = conn.execute(
            """
            SELECT job.import_id
            FROM fca_processing_jobs AS job
            JOIN collector_imports AS imported ON imported.import_id = job.import_id
            WHERE job.firm_id = ? AND job.source_record_hash = ?
              AND imported.state = 'accepted'
            ORDER BY job.id DESC LIMIT 1
            """,
            (firm_id, firm["source_record_hash"]),
        ).fetchone()
        if source is None:
            raise WebsiteResearchConflict("accepted FCA processing source is missing")
        latest = conn.execute(
            """
            SELECT id, action
            FROM firm_contact_evidence_events
            WHERE firm_id = ? ORDER BY id DESC LIMIT 1
            """,
            (firm_id,),
        ).fetchone()
        latest_id = int(latest["id"]) if latest else None
        if latest_id != expected_previous_event_id:
            raise WebsiteResearchConflict("contact evidence changed; refresh and try again")
        if action == "withdraw":
            if latest is None or latest["action"] != "assert":
                raise WebsiteResearchConflict("contact evidence is already withdrawn")
        event_id = conn.execute(
            """
            INSERT INTO firm_contact_evidence_events (
                firm_id, action, email, phone, contact_name, evidence_url,
                justification, actor, fca_source_record_hash, collector_import_id,
                expected_previous_event_id, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                firm_id,
                action,
                clean_email,
                clean_phone,
                clean_contact_name,
                evidence,
                reason,
                clean_actor,
                firm["source_record_hash"],
                source["import_id"],
                expected_previous_event_id,
                now.astimezone(UTC).isoformat(),
            ),
        ).lastrowid
        if owns_transaction:
            conn.execute("COMMIT")
    except Exception:
        if owns_transaction and conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    if event_id is None:
        raise RuntimeError("SQLite did not return a contact evidence event id")
    return int(event_id)
