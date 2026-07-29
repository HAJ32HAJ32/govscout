from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import os
from pathlib import Path
import sqlite3


LCA_SOURCE_REGISTER = "LCA member directory"
RETIREMENT_NOTE = "Legacy LCA candidate staging retired; no raw candidate data retained"
_BACKUP_PROOF = object()


@dataclass(frozen=True, slots=True)
class BackupReceipt:
    source_path: Path
    backup_path: Path
    backup_sha256: str
    table_counts: tuple[tuple[str, int], ...]
    _proof: object

    def __post_init__(self) -> None:
        if self._proof is not _BACKUP_PROOF:
            raise TypeError("backup receipts must be produced by create_verified_backup")


@dataclass(frozen=True, slots=True)
class RetirementResult:
    retired_count: int
    leads_count: int
    sends_count: int


def _database_path(conn: sqlite3.Connection) -> Path:
    row = conn.execute("PRAGMA database_list").fetchone()
    if row is None or not row[2]:
        raise ValueError("retirement requires a file-backed SQLite database")
    return Path(row[2]).resolve()


def _require_integrity(conn: sqlite3.Connection, label: str) -> None:
    rows = conn.execute("PRAGMA integrity_check").fetchall()
    if len(rows) != 1 or rows[0][0] != "ok":
        raise ValueError(f"{label} SQLite integrity check failed")


def _require_foreign_keys(conn: sqlite3.Connection, label: str) -> None:
    if conn.execute("PRAGMA foreign_key_check").fetchall():
        raise ValueError(f"{label} SQLite foreign key check failed")


def _table_counts(conn: sqlite3.Connection) -> tuple[tuple[str, int], ...]:
    names = [
        row[0]
        for row in conn.execute(
            """
            SELECT name FROM sqlite_schema
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        )
    ]
    counts = []
    for name in names:
        quoted = name.replace('"', '""')
        count = int(conn.execute(f'SELECT count(*) FROM "{quoted}"').fetchone()[0])
        counts.append((name, count))
    return tuple(counts)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_verified_backup(
    conn: sqlite3.Connection,
    backup_path: str | Path,
) -> BackupReceipt:
    """Create and verify a new local SQLite backup before destructive retirement."""
    if conn.in_transaction:
        raise sqlite3.OperationalError("backup requires no active transaction")
    source_path = _database_path(conn)
    destination_path = Path(backup_path).expanduser().resolve()
    if destination_path == source_path:
        raise ValueError("backup path must differ from the source database")
    destination_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination_path.exists():
        raise FileExistsError(f"backup path already exists: {destination_path}")

    _require_integrity(conn, "source")
    source_counts = _table_counts(conn)
    descriptor = os.open(destination_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    os.close(descriptor)
    try:
        backup = sqlite3.connect(destination_path, isolation_level=None)
        try:
            conn.backup(backup)
            _require_integrity(backup, "backup")
            if _table_counts(backup) != source_counts:
                raise ValueError("backup table counts do not match source")
        finally:
            backup.close()
        os.chmod(destination_path, 0o600)
        backup_hash = _file_sha256(destination_path)
    except Exception:
        destination_path.unlink(missing_ok=True)
        raise

    return BackupReceipt(
        source_path=source_path,
        backup_path=destination_path,
        backup_sha256=backup_hash,
        table_counts=source_counts,
        _proof=_BACKUP_PROOF,
    )


def _verify_backup_receipt(
    conn: sqlite3.Connection,
    receipt: BackupReceipt,
) -> None:
    if type(receipt) is not BackupReceipt or receipt._proof is not _BACKUP_PROOF:
        raise TypeError("a verified local backup receipt is required")
    if _database_path(conn) != receipt.source_path:
        raise ValueError("backup receipt belongs to a different source database")
    try:
        backup_hash = _file_sha256(receipt.backup_path)
    except OSError as exc:
        raise ValueError("backup receipt file is unavailable") from exc
    if backup_hash != receipt.backup_sha256:
        raise ValueError("backup receipt hash does not match the backup file")

    _require_integrity(conn, "source")
    if _table_counts(conn) != receipt.table_counts:
        raise ValueError("source database no longer matches the backup receipt")
    try:
        backup = sqlite3.connect(
            f"file:{receipt.backup_path}?mode=ro", uri=True, isolation_level=None
        )
        try:
            _require_integrity(backup, "backup")
            if _table_counts(backup) != receipt.table_counts:
                raise ValueError("backup table counts no longer match the receipt")
        finally:
            backup.close()
    except sqlite3.Error as exc:
        raise ValueError("backup receipt does not reference a valid SQLite backup") from exc


def retire_lca_candidates(
    conn: sqlite3.Connection,
    *,
    backup_receipt: BackupReceipt,
    now: datetime,
) -> RetirementResult:
    """Retire legacy LCA staging only after a current verified local backup."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if conn.in_transaction:
        raise sqlite3.OperationalError("LCA retirement requires no active transaction")
    _verify_backup_receipt(conn, backup_receipt)

    retired_at = now.astimezone(UTC).isoformat()
    try:
        conn.execute("BEGIN IMMEDIATE")
        candidates_before = int(
            conn.execute("SELECT count(*) FROM candidates").fetchone()[0]
        )
        leads_count = int(conn.execute("SELECT count(*) FROM leads").fetchone()[0])
        sends_count = int(conn.execute("SELECT count(*) FROM sends").fetchone()[0])
        retired_count = int(
            conn.execute(
                "SELECT count(*) FROM candidates WHERE source_register = ?",
                (LCA_SOURCE_REGISTER,),
            ).fetchone()[0]
        )
        _verify_backup_receipt(conn, backup_receipt)
        deleted = conn.execute(
            "DELETE FROM candidates WHERE source_register = ?",
            (LCA_SOURCE_REGISTER,),
        )
        candidates_after = int(
            conn.execute("SELECT count(*) FROM candidates").fetchone()[0]
        )
        if deleted.rowcount != retired_count or candidates_after != candidates_before - retired_count:
            raise RuntimeError("candidate retirement count verification failed")
        if int(conn.execute("SELECT count(*) FROM leads").fetchone()[0]) != leads_count:
            raise RuntimeError("lead count changed during candidate retirement")
        if int(conn.execute("SELECT count(*) FROM sends").fetchone()[0]) != sends_count:
            raise RuntimeError("send count changed during candidate retirement")
        _require_foreign_keys(conn, "post-retirement source")
        _require_integrity(conn, "post-retirement source")
        conn.execute(
            """
            INSERT INTO retirement_events (
                source_register, retired_count, leads_before, sends_before,
                backup_path, backup_sha256, retired_at, note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                LCA_SOURCE_REGISTER,
                retired_count,
                leads_count,
                sends_count,
                str(backup_receipt.backup_path),
                backup_receipt.backup_sha256,
                retired_at,
                RETIREMENT_NOTE,
            ),
        )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise

    return RetirementResult(retired_count, leads_count, sends_count)
