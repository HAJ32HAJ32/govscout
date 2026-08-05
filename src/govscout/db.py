from __future__ import annotations

from datetime import UTC, datetime
import hashlib
from importlib import resources
import os
from pathlib import Path
import sqlite3
import stat

from govscout.companies_house import VerifiedCompany, is_verified_company
from govscout.email_address import normalise_single_recipient


ALLOWED_LEGAL_FORMS = frozenset(
    {"ltd", "plc", "llp", "cic", "charitable_company"}
)


def connect_database(path: str | Path) -> sqlite3.Connection:
    database_path = Path(path)
    if str(database_path) != ":memory:":
        parent_existed = database_path.parent.exists()
        database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not parent_existed:
            os.chmod(database_path.parent, 0o700)
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        if database_path.is_symlink():
            raise OSError("database path must not be a symbolic link")
        try:
            descriptor = os.open(
                database_path,
                os.O_CREAT | os.O_EXCL | os.O_RDWR | no_follow,
                0o600,
            )
        except FileExistsError:
            if database_path.is_symlink():
                raise OSError("database path must not be a symbolic link") from None
            descriptor = os.open(database_path, os.O_RDWR | no_follow)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OSError("database path must be a regular file")
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
    conn = sqlite3.connect(database_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _migration_texts() -> tuple[tuple[str, str, str], ...]:
    directory = resources.files("govscout.resources").joinpath("migrations")
    migrations: list[tuple[str, str, str]] = []
    versions: set[str] = set()
    for migration in sorted(directory.iterdir(), key=lambda item: item.name):
        if not migration.name.endswith(".sql"):
            continue
        version, separator, _description = migration.name.partition("_")
        if not separator or not version.isdigit() or version in versions:
            raise RuntimeError(f"invalid or duplicate migration name: {migration.name}")
        versions.add(version)
        migrations.append(
            (version, migration.name, migration.read_text(encoding="utf-8"))
        )
    if not migrations:
        raise RuntimeError("no database migrations found")
    return tuple(migrations)


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
    migrations = _migration_texts()
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
        for version, _name, sql in migrations:
            checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            row = conn.execute(
                "SELECT checksum FROM schema_migrations WHERE version = ?",
                (version,),
            ).fetchone()
            if row:
                if row[0] != checksum:
                    raise RuntimeError(
                        f"migration checksum mismatch for version {version}"
                    )
                continue
            for statement in _statements(sql):
                conn.execute(statement)
            conn.execute(
                """
                INSERT INTO schema_migrations (version, checksum, applied_at)
                VALUES (?, ?, ?)
                """,
                (version, checksum, datetime.now(UTC).isoformat()),
            )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


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
    if not is_verified_company(company):
        raise TypeError("company must be verified Companies House evidence")
    if company.legal_form not in ALLOWED_LEGAL_FORMS:
        raise ValueError("only incorporated legal forms may enter GovScout")
    if company.verification_source != "companies_house_api":
        raise ValueError("Companies House API verification is required")
    normalized_email = normalise_single_recipient(contact_email)
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
