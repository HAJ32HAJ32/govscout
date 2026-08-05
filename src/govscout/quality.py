from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from govscout.db import ALLOWED_LEGAL_FORMS
from govscout.fca_discovery import FcaDataError, canonicalize_website_url

FCA_MAX_AGE = timedelta(days=30)
COMPANIES_HOUSE_MAX_AGE = timedelta(days=30)
ENRICHMENT_MAX_AGE = timedelta(days=14)
QC_VALIDITY = timedelta(days=7)


@dataclass(frozen=True, slots=True)
class QcResult:
    qc_run_id: int
    passed: bool
    reasons: tuple[str, ...]


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("database timestamp is not timezone-aware")
    return parsed.astimezone(UTC)


def _duplicate_snapshot(conn: sqlite3.Connection, firm: sqlite3.Row) -> list[dict[str, object]]:
    if not firm["website_url"]:
        return []
    rows = conn.execute(
        """
        SELECT id, frn, fca_status, is_active, website_url,
               source_record_hash, last_seen_at
        FROM fca_firms
        WHERE id != ? AND is_active = 1 AND website_url = ?
        ORDER BY id
        """,
        (firm["id"], firm["website_url"]),
    ).fetchall()
    return [dict(row) for row in rows]


def _duplicate_company_snapshot(
    conn: sqlite3.Connection, firm: sqlite3.Row
) -> list[dict[str, object]]:
    if firm["company_number"] is None:
        return []
    rows = conn.execute(
        """
        SELECT id, frn, firm_name, source_record_hash
        FROM fca_firms
        WHERE id <> ? AND is_active = 1 AND company_number = ?
        ORDER BY id
        """,
        (firm["id"], firm["company_number"]),
    ).fetchall()
    return [dict(row) for row in rows]


def _latest_company_verification(
    conn: sqlite3.Connection, firm: sqlite3.Row
) -> dict[str, object] | None:
    attempt = conn.execute(
        """
        SELECT id, company_number, state, reason_code, checked_at,
               fca_source_record_hash, legal_name, legal_form,
               company_status, profile_hash
        FROM company_verification_attempts
        WHERE firm_id = ? ORDER BY id DESC LIMIT 1
        """,
        (firm["id"],),
    ).fetchone()
    return dict(attempt) if attempt is not None else None


def _company_verification_reasons(
    verification: dict[str, object] | None,
    *,
    firm: sqlite3.Row,
    current: datetime,
) -> set[str]:
    if verification is None:
        return {"COMPANIES_HOUSE_VERIFICATION_MISSING"}
    reasons: set[str] = set()
    if verification["state"] != "verified":
        reasons.add("COMPANIES_HOUSE_VERIFICATION_FAILED")
        return reasons
    if verification["company_number"] != firm["company_number"]:
        reasons.add("COMPANIES_HOUSE_COMPANY_MISMATCH")
    if verification["fca_source_record_hash"] != firm["source_record_hash"]:
        reasons.add("SOURCE_CHANGED_SINCE_COMPANIES_HOUSE_VERIFICATION")
    if verification["company_status"] != "active":
        reasons.add("COMPANIES_HOUSE_NOT_ACTIVE")
    if verification["legal_form"] not in ALLOWED_LEGAL_FORMS:
        reasons.add("COMPANIES_HOUSE_LEGAL_FORM_INELIGIBLE")
    if not verification["profile_hash"]:
        reasons.add("COMPANIES_HOUSE_VERIFICATION_INVALID")
    try:
        verified_at = _utc(str(verification["checked_at"]))
    except (TypeError, ValueError):
        reasons.add("COMPANIES_HOUSE_VERIFICATION_INVALID")
    else:
        if verified_at > current or current - verified_at > COMPANIES_HOUSE_MAX_AGE:
            reasons.add("COMPANIES_HOUSE_VERIFICATION_STALE")
    return reasons


def company_verification_is_current(
    conn: sqlite3.Connection, *, firm_id: int, now: datetime
) -> bool:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    firm = conn.execute("SELECT * FROM fca_firms WHERE id = ?", (firm_id,)).fetchone()
    if firm is None:
        raise KeyError(firm_id)
    return not _company_verification_reasons(
        _latest_company_verification(conn, firm),
        firm=firm,
        current=now.astimezone(UTC),
    )


def _qc_input_hash(
    conn: sqlite3.Connection,
    firm: sqlite3.Row,
    run: sqlite3.Row | None,
    evidence: list[sqlite3.Row],
) -> str:
    firm_fields = (
        "id",
        "frn",
        "firm_name",
        "fca_status",
        "is_active",
        "source_url",
        "website_url",
        "company_number",
        "lead_id",
        "source_record_hash",
        "first_seen_at",
        "last_seen_at",
    )
    run_fields = (
        "id",
        "firm_id",
        "state",
        "started_at",
        "completed_at",
        "website_url",
        "final_url",
        "page_hash",
        "input_hash",
        "score",
        "temperature",
        "failure_code",
    )
    evidence_fields = (
        "id",
        "run_id",
        "signal_group",
        "code",
        "evidence_state",
        "weight",
        "source_url",
        "excerpt",
        "observed_at",
        "content_hash",
    )
    payload = {
        "firm": {field: firm[field] for field in firm_fields},
        "lead": (
            dict(conn.execute("SELECT * FROM leads WHERE id = ?", (firm["lead_id"],)).fetchone())
            if firm["lead_id"] is not None
            and conn.execute("SELECT 1 FROM leads WHERE id = ?", (firm["lead_id"],)).fetchone()
            else None
        ),
        "company_verification": _latest_company_verification(conn, firm),
        "duplicate_websites": _duplicate_snapshot(conn, firm),
        "duplicate_companies": _duplicate_company_snapshot(conn, firm),
        "enrichment": (
            {field: run[field] for field in run_fields} if run is not None else None
        ),
        "evidence": [
            {field: item[field] for field in evidence_fields} for item in evidence
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _latest_inputs(
    conn: sqlite3.Connection,
    *,
    firm_id: int,
) -> tuple[sqlite3.Row | None, list[sqlite3.Row]]:
    run = conn.execute(
        """
        SELECT * FROM enrichment_runs
        WHERE firm_id = ? ORDER BY id DESC LIMIT 1
        """,
        (firm_id,),
    ).fetchone()
    evidence: list[sqlite3.Row] = []
    if run is not None and run["state"] == "complete":
        evidence = list(
            conn.execute(
                "SELECT * FROM evidence_items WHERE run_id = ? ORDER BY id", (run["id"],)
            ).fetchall()
        )
    return run, evidence


def _evaluate_current(
    conn: sqlite3.Connection,
    *,
    firm: sqlite3.Row,
    run: sqlite3.Row | None,
    evidence: list[sqlite3.Row],
    current: datetime,
) -> set[str]:
    reasons: set[str] = set()
    if firm["is_active"] != 1:
        reasons.add("FCA_NOT_ACTIVE")
    if current - _utc(firm["last_seen_at"]) > FCA_MAX_AGE:
        reasons.add("FCA_EVIDENCE_STALE")
    if not firm["website_url"]:
        reasons.add("WEBSITE_MISSING")
    elif _duplicate_snapshot(conn, firm):
        reasons.add("DUPLICATE_WEBSITE")
    if _duplicate_company_snapshot(conn, firm):
        reasons.add("DUPLICATE_COMPANY_NUMBER")

    verification = _latest_company_verification(conn, firm)
    reasons.update(
        _company_verification_reasons(verification, firm=firm, current=current)
    )

    if run is None:
        reasons.add("SCAN_MISSING")
    elif run["state"] != "complete":
        reasons.add("SCAN_FAILED")
    else:
        if current - _utc(run["completed_at"]) > ENRICHMENT_MAX_AGE:
            reasons.add("SCAN_STALE")
        if run["input_hash"] != firm["source_record_hash"]:
            reasons.add("SOURCE_CHANGED_SINCE_SCAN")
        website_origin = urlsplit(firm["website_url"])
        final_origin = urlsplit(run["final_url"])
        try:
            canonical_final = canonicalize_website_url(run["final_url"])
        except FcaDataError:
            canonical_final = None
        if (
            run["website_url"] != firm["website_url"]
            or canonical_final != run["final_url"]
            or (
                website_origin.scheme,
                website_origin.hostname,
                website_origin.port,
            )
            != (
                final_origin.scheme,
                final_origin.hostname,
                final_origin.port,
            )
        ):
            reasons.add("WEBSITE_CHANGED_SINCE_SCAN")
        groups = {item["signal_group"] for item in evidence}
        if not {"accountability", "ai_exposure", "governance_gap"} <= groups:
            reasons.add("EVIDENCE_MISSING")
        states_by_code: dict[str, set[str]] = {}
        for item in evidence:
            states_by_code.setdefault(item["code"], set()).add(item["evidence_state"])
        if any(len(states) > 1 for states in states_by_code.values()):
            reasons.add("CONTRADICTORY_EVIDENCE")
        if any(
            item["evidence_state"] == "present"
            and (not item["source_url"] or not item["excerpt"])
            for item in evidence
        ):
            reasons.add("UNEVIDENCED_CLAIM")
        if any(item["evidence_state"] == "unknown" for item in evidence):
            reasons.add("EVIDENCE_UNKNOWN")
    return reasons


def run_qc(conn: sqlite3.Connection, *, firm_id: int, now: datetime) -> QcResult:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if conn.in_transaction:
        raise sqlite3.OperationalError("QC requires no active transaction")
    current = now.astimezone(UTC)
    try:
        conn.execute("BEGIN IMMEDIATE")
        firm = conn.execute("SELECT * FROM fca_firms WHERE id = ?", (firm_id,)).fetchone()
        if firm is None:
            raise KeyError(firm_id)
        run, evidence = _latest_inputs(conn, firm_id=firm_id)
        ordered_reasons = tuple(
            sorted(
                _evaluate_current(
                    conn, firm=firm, run=run, evidence=evidence, current=current
                )
            )
        )
        passed = not ordered_reasons
        reason_json = json.dumps(ordered_reasons, separators=(",", ":"))
        input_hash = _qc_input_hash(conn, firm, run, evidence)
        verification = _latest_company_verification(conn, firm)
        verification_attempt_id = (
            verification["id"]
            if verification is not None and verification["state"] == "verified"
            else None
        )
        checked = current.isoformat()
        expires = (current + QC_VALIDITY).isoformat()
        qc_run_id = conn.execute(
            """
            INSERT INTO qc_runs (
                firm_id, enrichment_run_id, state, reason_codes,
                input_hash, checked_at, expires_at, company_verification_attempt_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                firm_id,
                run["id"] if run is not None and run["state"] == "complete" else None,
                "pass" if passed else "fail",
                reason_json,
                input_hash,
                checked,
                expires,
                verification_attempt_id,
            ),
        ).lastrowid
        if qc_run_id is None:
            raise RuntimeError("SQLite did not return a QC run id")
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return QcResult(int(qc_run_id), passed, ordered_reasons)


def qc_is_current(
    conn: sqlite3.Connection,
    *,
    firm_id: int,
    qc_run_id: int,
    now: datetime,
) -> bool:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    current = now.astimezone(UTC)
    qc = conn.execute(
        "SELECT * FROM qc_runs WHERE id = ? AND firm_id = ?", (qc_run_id, firm_id)
    ).fetchone()
    if (
        qc is None
        or qc["state"] != "pass"
        or _utc(qc["checked_at"]) > current
        or _utc(qc["expires_at"]) <= current
    ):
        return False
    firm = conn.execute("SELECT * FROM fca_firms WHERE id = ?", (firm_id,)).fetchone()
    if firm is None:
        return False
    verification = _latest_company_verification(conn, firm)
    if (
        verification is None
        or verification["state"] != "verified"
        or verification["id"] is None
        or verification["id"] != qc["company_verification_attempt_id"]
    ):
        return False
    run, evidence = _latest_inputs(conn, firm_id=firm_id)
    if (
        run is None
        or run["state"] != "complete"
        or run["id"] != qc["enrichment_run_id"]
        or _evaluate_current(
            conn, firm=firm, run=run, evidence=evidence, current=current
        )
    ):
        return False
    return qc["input_hash"] == _qc_input_hash(conn, firm, run, evidence)


def review_firm(
    conn: sqlite3.Connection,
    *,
    firm_id: int,
    decision: str,
    qc_run_id: int | None,
    notes: str | None,
    rejection_reason: str | None,
    now: datetime,
) -> None:
    if decision not in {"approved", "rejected"}:
        raise ValueError("decision must be approved or rejected")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if conn.in_transaction:
        raise sqlite3.OperationalError("firm review requires no active transaction")
    try:
        conn.execute("BEGIN IMMEDIATE")
        archive = conn.execute(
            """
            SELECT id, action FROM firm_archive_events
            WHERE firm_id = ? ORDER BY id DESC LIMIT 1
            """,
            (firm_id,),
        ).fetchone()
        if archive is not None and archive["action"] == "archive":
            raise ValueError("archived firms cannot be reviewed")
        if decision == "approved":
            if qc_run_id is None or not qc_is_current(
                conn, firm_id=firm_id, qc_run_id=qc_run_id, now=now
            ):
                raise ValueError("approval requires passing current QC")
            rejection_reason = None
        elif not rejection_reason or not rejection_reason.strip():
            raise ValueError("rejection requires a reason")
        conn.execute(
            """
            INSERT INTO firm_reviews (
                firm_id, decision, qc_run_id, notes, rejection_reason, reviewed_at,
                archive_event_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                firm_id,
                decision,
                qc_run_id if decision == "approved" else None,
                notes.strip() if notes and notes.strip() else None,
                rejection_reason.strip() if rejection_reason else None,
                now.astimezone(UTC).isoformat(),
                int(archive["id"]) if archive is not None else None,
            ),
        )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def is_outreach_ready(conn: sqlite3.Connection, *, firm_id: int, now: datetime) -> bool:
    archive = conn.execute(
        """
        SELECT id, action FROM firm_archive_events
        WHERE firm_id = ? ORDER BY id DESC LIMIT 1
        """,
        (firm_id,),
    ).fetchone()
    if archive is not None and archive["action"] == "archive":
        return False
    firm = conn.execute(
        """
        SELECT f.lead_id, l.contact_email
        FROM fca_firms f
        LEFT JOIN leads l ON l.id = f.lead_id
        WHERE f.id = ?
        """,
        (firm_id,),
    ).fetchone()
    if firm is None or firm["lead_id"] is None or not firm["contact_email"]:
        return False
    review = conn.execute(
        """
        SELECT decision, qc_run_id, archive_event_id FROM firm_reviews
        WHERE firm_id = ? ORDER BY id DESC LIMIT 1
        """,
        (firm_id,),
    ).fetchone()
    return bool(
        review
        and review["decision"] == "approved"
        and review["qc_run_id"] is not None
        and review["archive_event_id"]
        == (int(archive["id"]) if archive is not None else None)
        and qc_is_current(conn, firm_id=firm_id, qc_run_id=review["qc_run_id"], now=now)
    )
