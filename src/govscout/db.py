from __future__ import annotations

from datetime import UTC, datetime
import hashlib
from importlib import resources
import os
from pathlib import Path
import sqlite3

from govscout.companies_house import VerifiedCompany


ALLOWED_LEGAL_FORMS = frozenset(
    {"ltd", "plc", "llp", "cic", "charitable_company"}
)
MIGRATION_VERSION = "001"
MIGRATION_NAME = "001_p1_ledger_and_core_leads.sql"


def connect_database(path: str | Path) -> sqlite3.Connection:
    database_path = Path(path)
    if str(database_path) != ":memory:":
        parent_existed = database_path.parent.exists()
        database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not parent_existed:
            os.chmod(database_path.parent, 0o700)
        if not database_path.exists():
            descriptor = os.open(
                database_path,
                os.O_CREAT | os.O_EXCL | os.O_RDWR,
                0o600,
            )
            os.close(descriptor)
        os.chmod(database_path, 0o600)
    conn = sqlite3.connect(database_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _migration_text() -> str:
    return (
        resources.files("govscout.resources")
        .joinpath("migrations", MIGRATION_NAME)
        .read_text(encoding="utf-8")
    )


def _statements(sql: str):
    buffer = ""
    for line in sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                yield statement
            buffer = ""
    if buffer.strip():
        raise RuntimeError("migration contains an incomplete SQL statement")


def migrate(conn: sqlite3.Connection) -> None:
    sql = _migration_text()
    checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                checksum TEXT NOT NULL CHECK (
                    length(checksum) = 64
                    AND checksum NOT GLOB '*[^0-9a-f]*'
                ),
                applied_at TEXT NOT NULL CHECK (substr(applied_at, -6) = '+00:00')
            )
            """
        )
        row = conn.execute(
            "SELECT checksum FROM schema_migrations WHERE version = ?",
            (MIGRATION_VERSION,),
        ).fetchone()
        if row:
            if row[0] != checksum:
                raise RuntimeError("migration checksum mismatch for version 001")
            conn.execute("COMMIT")
            return
        for statement in _statements(sql):
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_migrations (version, checksum, applied_at) VALUES (?, ?, ?)",
            (MIGRATION_VERSION, checksum, datetime.now(UTC).isoformat()),
        )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _single_recipient_email(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("contact_email must be a single recipient address")
    email = value.strip().lower()
    if (
        email.count("@") != 1
        or any(character in email for character in "\r\n,; \t")
        or not all(email.split("@", 1))
    ):
        raise ValueError("contact_email must be a single recipient address")
    return email


def insert_verified_lead(
    conn: sqlite3.Connection,
    *,
    company: VerifiedCompany,
    contact_email: str,
    source_register: str,
    contact_name: str | None = None,
    eu_facing: bool = False,
    now: datetime | None = None,
) -> int:
    if not isinstance(company, VerifiedCompany):
        raise TypeError("company must be verified Companies House evidence")
    if company.legal_form not in ALLOWED_LEGAL_FORMS:
        raise ValueError("only incorporated legal forms may enter GovScout")
    if company.verification_source != "companies_house_api":
        raise ValueError("Companies House API verification is required")
    normalized_email = _single_recipient_email(contact_email)
    if not source_register.strip():
        raise ValueError("source_register is required")

    created_at = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
    cursor = conn.execute(
        """
        INSERT INTO leads (
            company_number, legal_name, legal_form, company_status,
            verification_source, companies_house_verified_at,
            companies_house_profile_hash, contact_name, contact_email,
            source_register, eu_facing, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            company.company_number,
            company.legal_name,
            company.legal_form,
            company.company_status,
            company.verification_source,
            company.verified_at.astimezone(UTC).isoformat(),
            company.profile_hash,
            contact_name.strip() if contact_name else None,
            normalized_email,
            source_register.strip(),
            int(eu_facing),
            created_at,
        ),
    )
    if cursor.lastrowid is None:
        raise RuntimeError("SQLite did not return a lead id")
    return cursor.lastrowid
