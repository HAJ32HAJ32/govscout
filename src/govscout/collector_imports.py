from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from govscout.fca_discovery import (
    FcaDataError,
    _ingest_fca_records_in_transaction,
    parse_fca_json,
)

COLLECTOR_BATCH_LIMIT = 25


@dataclass(frozen=True, slots=True)
class CollectorImportResult:
    import_id: str
    state: str
    source_count: int
    staged_count: int
    created_count: int
    changed_count: int
    error_code: str | None = None


def _accepted_result(import_id: str, result_json: str) -> CollectorImportResult:
    result = json.loads(result_json)
    return CollectorImportResult(
        import_id=import_id,
        state="accepted",
        source_count=result["source_count"],
        staged_count=result["staged_count"],
        created_count=result["created_count"],
        changed_count=result["changed_count"],
    )


def _rejected_result(import_id: str, error_code: str) -> CollectorImportResult:
    return CollectorImportResult(
        import_id=import_id,
        state="rejected",
        source_count=0,
        staged_count=0,
        created_count=0,
        changed_count=0,
        error_code=error_code,
    )


def process_collector_import(
    conn: sqlite3.Connection,
    *,
    import_id: str,
    now: datetime,
) -> CollectorImportResult:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("collector import time must be timezone-aware")
    if conn.in_transaction:
        raise sqlite3.OperationalError("collector processing requires no active transaction")
    processed_at = now.astimezone(UTC)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT state, payload_json, received_at, result_json, error_code
            FROM collector_imports WHERE import_id = ?
            """,
            (import_id,),
        ).fetchone()
        if row is None:
            raise KeyError("collector import not found")
        if row["state"] == "accepted":
            result = _accepted_result(import_id, row["result_json"])
            conn.execute("COMMIT")
            return result
        if row["state"] == "rejected":
            result = _rejected_result(import_id, row["error_code"])
            conn.execute("COMMIT")
            return result
        if processed_at < datetime.fromisoformat(row["received_at"]):
            raise ValueError("collector import cannot be processed before receipt")
        conn.execute("SAVEPOINT collector_ingest")
        try:
            records = parse_fca_json(row["payload_json"].encode("utf-8"))
            if len(records) > COLLECTOR_BATCH_LIMIT:
                error_code = "BATCH_LIMIT_EXCEEDED"
                conn.execute(
                    """
                    UPDATE collector_imports
                    SET state = 'rejected', processed_at = ?, error_code = ?
                    WHERE import_id = ? AND state = 'pending'
                    """,
                    (processed_at.isoformat(), error_code, import_id),
                )
                conn.execute("RELEASE SAVEPOINT collector_ingest")
                conn.execute("COMMIT")
                return _rejected_result(import_id, error_code)
            ingested = _ingest_fca_records_in_transaction(
                conn,
                records,
                limit=COLLECTOR_BATCH_LIMIT,
                now=processed_at,
            )
        except FcaDataError:
            conn.execute("ROLLBACK TO SAVEPOINT collector_ingest")
            conn.execute("RELEASE SAVEPOINT collector_ingest")
            error_code = "FCA_DATA_REJECTED"
            conn.execute(
                """
                UPDATE collector_imports
                SET state = 'rejected', processed_at = ?, error_code = ?
                WHERE import_id = ? AND state = 'pending'
                """,
                (processed_at.isoformat(), error_code, import_id),
            )
            conn.execute("COMMIT")
            return _rejected_result(import_id, error_code)
        conn.execute("RELEASE SAVEPOINT collector_ingest")
        result_payload = json.dumps(
            {
                "source_count": ingested.source_count,
                "staged_count": ingested.staged_count,
                "created_count": ingested.created_count,
                "changed_count": ingested.changed_count,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        conn.execute(
            """
            UPDATE collector_imports
            SET state = 'accepted', processed_at = ?, result_json = ?
            WHERE import_id = ? AND state = 'pending'
            """,
            (processed_at.isoformat(), result_payload, import_id),
        )
        result = _accepted_result(import_id, result_payload)
        conn.execute("COMMIT")
        return result
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
