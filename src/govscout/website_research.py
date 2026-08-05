from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import sqlite3
from urllib.parse import urlsplit, urlunsplit


VERIFICATION_MAX_AGE = timedelta(days=30)


class WebsiteResearchConflict(RuntimeError):
    """The operator acted on stale or ineligible website research state."""


@dataclass(frozen=True, slots=True)
class ReprocessingRequest:
    job_id: int
    input_hash: str
    created: bool


@dataclass(frozen=True, slots=True)
class CurrentReprocessingInput:
    job_id: int
    firm_id: int
    source_record_hash: str
    website_url: str
    website_evidence_event_id: int
    company_verification_attempt_id: int
    input_hash: str


def _printable_ascii(value: str, *, field: str, minimum: int, maximum: int) -> str:
    clean = value.strip()
    if not minimum <= len(clean) <= maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum} characters")
    if any(not 32 <= ord(character) <= 126 for character in clean):
        raise ValueError(f"{field} must contain printable ASCII characters only")
    return clean


def _canonical_url(raw: str, *, field: str, allow_query: bool) -> str:
    value = raw.strip()
    if not value or len(value) > 2048:
        raise ValueError(f"{field} is required and must be at most 2048 characters")
    if any(ord(character) <= 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{field} must not contain control characters")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{field} must be a valid HTTPS URL") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.fragment
        or (parsed.query and not allow_query)
        or parsed.hostname != parsed.hostname.lower()
        or ".." in parsed.hostname
        or parsed.hostname.startswith((".", "-"))
        or parsed.hostname.endswith((".", "-"))
        or any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or any(not (character.isascii() and (character.isalnum() or character == "-")) for character in label)
            for label in parsed.hostname.split(".")
        )
    ):
        raise ValueError(f"{field} must be a canonical HTTPS URL")
    path = parsed.path or "/"
    if "//" in path or "/./" in path or "/../" in path or path.endswith(("/.", "/..")):
        raise ValueError(f"{field} path must be canonical")
    canonical = urlunsplit(("https", parsed.hostname, path, parsed.query, ""))
    if field == "website URL" and canonical != value:
        raise ValueError("website URL must already be canonical")
    return canonical


def record_website_evidence(
    conn: sqlite3.Connection,
    *,
    firm_id: int,
    action: str,
    website_url: str,
    evidence_url: str,
    justification: str,
    actor: str,
    expected_previous_event_id: int | None,
    now: datetime,
) -> int:
    if action not in {"assert", "withdraw"}:
        raise ValueError("website evidence action must be assert or withdraw")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    website = _canonical_url(website_url, field="website URL", allow_query=False)
    evidence = _canonical_url(evidence_url, field="evidence URL", allow_query=True)
    reason = _printable_ascii(
        justification, field="website evidence justification", minimum=10, maximum=1000
    )
    clean_actor = _printable_ascii(actor, field="website evidence actor", minimum=1, maximum=100)
    if expected_previous_event_id is not None and expected_previous_event_id <= 0:
        raise ValueError("expected website evidence event id must be positive")
    if conn.in_transaction:
        raise sqlite3.OperationalError("website evidence action requires no active transaction")
    try:
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
            SELECT id, action, website_url
            FROM firm_website_evidence_events
            WHERE firm_id = ? ORDER BY id DESC LIMIT 1
            """,
            (firm_id,),
        ).fetchone()
        latest_id = int(latest["id"]) if latest else None
        if latest_id != expected_previous_event_id:
            raise WebsiteResearchConflict("website evidence changed; refresh and try again")
        if action == "withdraw":
            if latest is None or latest["action"] != "assert":
                raise WebsiteResearchConflict("website evidence is already withdrawn")
            if website != latest["website_url"]:
                raise WebsiteResearchConflict("withdrawal website changed; refresh and try again")
        event_id = conn.execute(
            """
            INSERT INTO firm_website_evidence_events (
                firm_id, action, website_url, evidence_url, justification,
                actor, fca_source_record_hash, collector_import_id,
                expected_previous_event_id, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                firm_id,
                action,
                website,
                evidence,
                reason,
                clean_actor,
                firm["source_record_hash"],
                source["import_id"],
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
        raise RuntimeError("SQLite did not return a website evidence event id")
    return int(event_id)


def _input_payload(
    *,
    firm: sqlite3.Row,
    evidence: sqlite3.Row,
    attempt: sqlite3.Row,
    source_job: sqlite3.Row,
) -> dict[str, object]:
    return {
        "v": 1,
        "fca": {
            key: firm[key]
            for key in (
                "id", "frn", "firm_name", "fca_status", "is_active",
                "source_url", "company_number", "source_record_hash",
            )
        },
        "website_evidence": {
            key: evidence[key]
            for key in (
                "id", "action", "website_url", "evidence_url",
                "justification", "actor", "fca_source_record_hash",
                "collector_import_id", "occurred_at",
            )
        },
        "company_verification": {
            key: attempt[key]
            for key in (
                "id", "company_number", "state", "checked_at",
                "fca_source_record_hash", "legal_name", "legal_form",
                "company_status", "profile_hash",
            )
        },
        "source_job": {
            key: source_job[key]
            for key in ("id", "import_id", "source_record_hash")
        },
    }


def _payload_hash(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def enqueue_website_reprocessing(
    conn: sqlite3.Connection,
    *,
    firm_id: int,
    expected_website_evidence_event_id: int,
    requested_by: str,
    request_reason: str,
    now: datetime,
) -> ReprocessingRequest:
    if expected_website_evidence_event_id <= 0:
        raise ValueError("expected website evidence event id must be positive")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    actor = _printable_ascii(
        requested_by, field="reprocessing requester", minimum=1, maximum=100
    )
    reason = _printable_ascii(
        request_reason, field="reprocessing reason", minimum=10, maximum=500
    )
    now_utc = now.astimezone(UTC)
    if conn.in_transaction:
        raise sqlite3.OperationalError("reprocessing enqueue requires no active transaction")
    try:
        conn.execute("BEGIN IMMEDIATE")
        firm = conn.execute(
            "SELECT * FROM fca_firms WHERE id = ?", (firm_id,)
        ).fetchone()
        if firm is None:
            raise KeyError(firm_id)
        if not firm["is_active"]:
            raise WebsiteResearchConflict("inactive firm cannot be reprocessed")
        archive = conn.execute(
            """
            SELECT action FROM firm_archive_events
            WHERE firm_id = ? ORDER BY id DESC LIMIT 1
            """,
            (firm_id,),
        ).fetchone()
        if archive is not None and archive["action"] == "archive":
            raise WebsiteResearchConflict("archived firm cannot be reprocessed")
        evidence = conn.execute(
            """
            SELECT * FROM firm_website_evidence_events
            WHERE firm_id = ? ORDER BY id DESC LIMIT 1
            """,
            (firm_id,),
        ).fetchone()
        if evidence is None or int(evidence["id"]) != expected_website_evidence_event_id:
            raise WebsiteResearchConflict("website evidence changed; refresh and try again")
        if evidence["action"] != "assert":
            raise WebsiteResearchConflict("website evidence was withdrawn")
        if evidence["fca_source_record_hash"] != firm["source_record_hash"]:
            raise WebsiteResearchConflict("FCA identity changed; refresh research")
        attempt = conn.execute(
            """
            SELECT * FROM company_verification_attempts
            WHERE firm_id = ? ORDER BY id DESC LIMIT 1
            """,
            (firm_id,),
        ).fetchone()
        if (
            attempt is None
            or attempt["state"] != "verified"
            or attempt["company_number"] != firm["company_number"]
            or attempt["fca_source_record_hash"] != firm["source_record_hash"]
        ):
            raise WebsiteResearchConflict("current Companies House verification is required")
        checked_at = datetime.fromisoformat(attempt["checked_at"]).astimezone(UTC)
        if checked_at > now_utc or now_utc - checked_at > VERIFICATION_MAX_AGE:
            raise WebsiteResearchConflict("Companies House verification is stale or future-dated")
        source_job = conn.execute(
            """
            SELECT job.*
            FROM fca_processing_jobs AS job
            JOIN collector_imports AS imported ON imported.import_id = job.import_id
            WHERE job.firm_id = ?
              AND job.source_record_hash = ?
              AND job.import_id = ?
              AND imported.state = 'accepted'
            ORDER BY job.id DESC LIMIT 1
            """,
            (
                firm_id,
                firm["source_record_hash"],
                evidence["collector_import_id"],
            ),
        ).fetchone()
        if source_job is None:
            raise WebsiteResearchConflict("accepted FCA processing source is missing")
        input_hash = _payload_hash(
            _input_payload(
                firm=firm,
                evidence=evidence,
                attempt=attempt,
                source_job=source_job,
            )
        )
        timestamp = now_utc.isoformat()
        cursor = conn.execute(
            """
            INSERT INTO fca_reprocessing_jobs (
                firm_id, source_job_id, source_record_hash,
                website_evidence_event_id, company_verification_attempt_id,
                input_hash, requested_by, request_reason, state,
                attempt_count, available_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)
            ON CONFLICT(firm_id, input_hash) DO NOTHING
            """,
            (
                firm_id,
                source_job["id"],
                firm["source_record_hash"],
                evidence["id"],
                attempt["id"],
                input_hash,
                actor,
                reason,
                timestamp,
                timestamp,
                timestamp,
            ),
        )
        created = cursor.rowcount == 1
        row = conn.execute(
            """
            SELECT id FROM fca_reprocessing_jobs
            WHERE firm_id = ? AND input_hash = ?
            """,
            (firm_id, input_hash),
        ).fetchone()
        if row is None:
            raise RuntimeError("reprocessing job insert was not observable")
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return ReprocessingRequest(int(row["id"]), input_hash, created)


def load_current_reprocessing_input(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    now: datetime,
) -> CurrentReprocessingInput:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    now_utc = now.astimezone(UTC)
    job = conn.execute(
        "SELECT * FROM fca_reprocessing_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if job is None:
        raise KeyError(job_id)
    firm = conn.execute(
        "SELECT * FROM fca_firms WHERE id = ?", (job["firm_id"],)
    ).fetchone()
    if firm is None or not firm["is_active"]:
        raise WebsiteResearchConflict("FCA identity changed")
    archive = conn.execute(
        """
        SELECT action FROM firm_archive_events
        WHERE firm_id = ? ORDER BY id DESC LIMIT 1
        """,
        (job["firm_id"],),
    ).fetchone()
    if archive is not None and archive["action"] == "archive":
        raise WebsiteResearchConflict("firm was archived")
    evidence = conn.execute(
        """
        SELECT * FROM firm_website_evidence_events
        WHERE firm_id = ? ORDER BY id DESC LIMIT 1
        """,
        (job["firm_id"],),
    ).fetchone()
    if (
        evidence is None
        or evidence["id"] != job["website_evidence_event_id"]
        or evidence["action"] != "assert"
        or evidence["fca_source_record_hash"] != firm["source_record_hash"]
        or job["source_record_hash"] != firm["source_record_hash"]
    ):
        raise WebsiteResearchConflict("website evidence changed")
    attempt = conn.execute(
        """
        SELECT * FROM company_verification_attempts
        WHERE firm_id = ? ORDER BY id DESC LIMIT 1
        """,
        (job["firm_id"],),
    ).fetchone()
    if (
        attempt is None
        or attempt["id"] != job["company_verification_attempt_id"]
        or attempt["state"] != "verified"
        or attempt["company_number"] != firm["company_number"]
        or attempt["fca_source_record_hash"] != firm["source_record_hash"]
    ):
        raise WebsiteResearchConflict("Companies House verification changed")
    checked_at = datetime.fromisoformat(attempt["checked_at"]).astimezone(UTC)
    if checked_at > now_utc or now_utc - checked_at > VERIFICATION_MAX_AGE:
        raise WebsiteResearchConflict("Companies House verification is stale or future-dated")
    source_job = conn.execute(
        "SELECT * FROM fca_processing_jobs WHERE id = ?", (job["source_job_id"],)
    ).fetchone()
    if (
        source_job is None
        or source_job["firm_id"] != job["firm_id"]
        or source_job["source_record_hash"] != firm["source_record_hash"]
    ):
        raise WebsiteResearchConflict("FCA processing source changed")
    expected_hash = _payload_hash(
        _input_payload(
            firm=firm,
            evidence=evidence,
            attempt=attempt,
            source_job=source_job,
        )
    )
    if expected_hash != job["input_hash"]:
        raise WebsiteResearchConflict("reprocessing input hash mismatch")
    return CurrentReprocessingInput(
        int(job["id"]),
        int(job["firm_id"]),
        job["source_record_hash"],
        evidence["website_url"],
        int(evidence["id"]),
        int(attempt["id"]),
        job["input_hash"],
    )
