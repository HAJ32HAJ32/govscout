from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import hashlib
import json
import re
import sqlite3
from typing import Sequence
from urllib.parse import parse_qs, urlsplit


FCA_MAX_RESPONSE_BYTES = 1_000_000
FCA_REGISTER_HOST = "register.fca.org.uk"
ACTIVE_FCA_STATUSES = frozenset(
    {"Authorised", "Registered", "Appointed Representative"}
)
_FRN = re.compile(r"^[0-9]{6,8}$")
_COMPANY_NUMBER = re.compile(r"^[A-Z0-9]{8}$")


class FcaDataError(ValueError):
    """The FCA source payload cannot be treated as authoritative evidence."""


@dataclass(frozen=True, slots=True)
class FcaFirmRecord:
    frn: str
    firm_name: str
    fca_status: str
    firm_type: str | None
    source_url: str
    website_url: str | None
    source_location: str | None
    company_number: str | None
    is_active: bool


@dataclass(frozen=True, slots=True)
class IngestResult:
    source_count: int
    staged_count: int
    created_count: int
    changed_count: int


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str or not value.strip():
        raise FcaDataError(f"{field} must be non-blank text or null")
    return value.strip()


def _validate_source_url(value: object, frn: str) -> str:
    if type(value) is not str:
        raise FcaDataError("source_url must be text")
    parsed = urlsplit(value)
    query = parse_qs(parsed.query, keep_blank_values=True)
    if not (
        parsed.scheme == "https"
        and parsed.hostname == FCA_REGISTER_HOST
        and parsed.port is None
        and parsed.username is None
        and parsed.password is None
        and parsed.path == "/s/firm"
        and query == {"id": [frn]}
        and not parsed.fragment
    ):
        raise FcaDataError("source_url must be the matching FCA Register firm page")
    return value


def _validate_website(value: object) -> str | None:
    website = _optional_text(value, "website_url")
    if website is None:
        return None
    parsed = urlsplit(website)
    if not (
        parsed.scheme == "https"
        and parsed.hostname
        and parsed.port in (None, 443)
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    ):
        raise FcaDataError("website_url must be a plain HTTPS URL")
    return website


def _parse_record(raw: object) -> FcaFirmRecord:
    if type(raw) is not dict:
        raise FcaDataError("each FCA firm must be an object")
    frn_value = raw.get("frn")
    if type(frn_value) is not str or not _FRN.fullmatch(frn_value):
        raise FcaDataError("FRN must contain 6 to 8 digits")
    name = _optional_text(raw.get("firm_name"), "firm_name")
    status = _optional_text(raw.get("status"), "status")
    assert name is not None and status is not None
    company_number = _optional_text(raw.get("company_number"), "company_number")
    if company_number is not None:
        company_number = company_number.upper()
        if not _COMPANY_NUMBER.fullmatch(company_number):
            raise FcaDataError("company_number must be 8 alphanumeric characters")
    return FcaFirmRecord(
        frn=frn_value,
        firm_name=name,
        fca_status=status,
        firm_type=_optional_text(raw.get("firm_type"), "firm_type"),
        source_url=_validate_source_url(raw.get("source_url"), frn_value),
        website_url=_validate_website(raw.get("website_url")),
        source_location=_optional_text(raw.get("location"), "location"),
        company_number=company_number,
        is_active=status in ACTIVE_FCA_STATUSES,
    )


def parse_fca_json(payload: bytes) -> tuple[FcaFirmRecord, ...]:
    if type(payload) is not bytes:
        raise TypeError("FCA payload must be bytes")
    if len(payload) > FCA_MAX_RESPONSE_BYTES:
        raise FcaDataError("FCA payload exceeded 1 MB")
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FcaDataError("FCA payload was not valid UTF-8 JSON") from exc
    if type(document) is not dict or type(document.get("firms")) is not list:
        raise FcaDataError("FCA payload must contain a firms list")
    records = tuple(_parse_record(item) for item in document["firms"])
    if not records:
        raise FcaDataError("FCA payload contained no firms")
    frns = [record.frn for record in records]
    if len(frns) != len(set(frns)):
        raise FcaDataError("FCA payload contained a duplicate FRN")
    return records


def _canonical_record(record: FcaFirmRecord) -> str:
    return json.dumps(
        asdict(record), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )


def ingest_fca_records(
    conn: sqlite3.Connection,
    records: Sequence[FcaFirmRecord],
    *,
    limit: int,
    now: datetime,
) -> IngestResult:
    if not 1 <= limit <= 100:
        raise ValueError("FCA ingestion limit must be between 1 and 100")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if not records:
        raise FcaDataError("FCA payload contained no firms")
    if conn.in_transaction:
        raise sqlite3.OperationalError("FCA ingestion requires no active transaction")
    ordered = sorted(records, key=lambda item: item.frn)
    staged_count = min(limit, len(ordered))
    if staged_count == 1:
        selected = (ordered[0],)
    else:
        selected = tuple(
            ordered[round(index * (len(ordered) - 1) / (staged_count - 1))]
            for index in range(staged_count)
        )
    observed_at = now.astimezone(UTC).isoformat()
    created_count = 0
    changed_count = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        for record in selected:
            canonical = _canonical_record(record)
            record_hash = hashlib.sha256(canonical.encode()).hexdigest()
            existing = conn.execute(
                "SELECT id, source_record_hash, last_seen_at FROM fca_firms WHERE frn = ?",
                (record.frn,),
            ).fetchone()
            if existing is not None and existing["last_seen_at"] > observed_at:
                raise FcaDataError(f"FCA observation for FRN {record.frn} is stale")
            values = (
                record.firm_name,
                record.fca_status,
                record.firm_type,
                int(record.is_active),
                record.source_url,
                record.website_url,
                record.source_location,
                record.company_number,
                record_hash,
                observed_at,
            )
            if existing is None:
                firm_id = conn.execute(
                    """
                    INSERT INTO fca_firms (
                        frn, firm_name, fca_status, firm_type, is_active,
                        source_url, website_url, source_location, company_number,
                        source_record_hash, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (record.frn, *values, observed_at),
                ).lastrowid
                created_count += 1
            else:
                firm_id = existing["id"]
                if existing["source_record_hash"] != record_hash:
                    changed_count += 1
                conn.execute(
                    """
                    UPDATE fca_firms SET
                        firm_name = ?, fca_status = ?, firm_type = ?, is_active = ?,
                        source_url = ?, website_url = ?, source_location = ?,
                        company_number = ?, source_record_hash = ?, last_seen_at = ?
                    WHERE id = ?
                    """,
                    (*values, firm_id),
                )
            conn.execute(
                """
                INSERT OR IGNORE INTO fca_observations (
                    firm_id, observed_at, source_record_hash, canonical_record
                ) VALUES (?, ?, ?, ?)
                """,
                (firm_id, observed_at, record_hash, canonical),
            )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return IngestResult(len(records), staged_count, created_count, changed_count)
