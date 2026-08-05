from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlencode, urlsplit

FCA_MAX_RESPONSE_BYTES = 1_000_000
FCA_MAX_CANONICAL_RECORD_CHARS = 32_768
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
        and (
            (parsed.path == "/s/firm" and query == {"id": [frn]})
            or (
                parsed.path == "/s/search"
                and query == {"q": [frn], "type": ["Companies"]}
            )
        )
        and not parsed.fragment
    ):
        raise FcaDataError("source_url must be the matching FCA Register firm search")
    return value


def fca_register_search_url(frn: str) -> str:
    if not _FRN.fullmatch(frn):
        raise ValueError("FRN must contain 6 to 8 digits")
    query = urlencode({"q": frn, "type": "Companies"})
    return f"https://{FCA_REGISTER_HOST}/s/search?{query}"


def canonicalize_website_url(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str or not value or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise FcaDataError("website_url contains forbidden characters")
    website = value
    parsed = urlsplit(website)
    try:
        port = parsed.port
    except ValueError as exc:
        raise FcaDataError("website_url must be a plain HTTPS URL") from exc
    if not (parsed.scheme.casefold() == "https" and parsed.hostname and port in (None, 443)):
        raise FcaDataError("website_url must be a plain HTTPS URL")
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise FcaDataError("website_url must not contain credentials, query, or fragment")
    if "\\" in parsed.path or "%" in parsed.path or any(ord(char) > 126 for char in parsed.path):
        raise FcaDataError("website_url path is not canonicalizable")
    try:
        if parsed.hostname.endswith("."):
            raise FcaDataError("website_url host must not have a trailing dot")
        host = parsed.hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise FcaDataError("website_url host is invalid") from exc
    allowed_host_characters = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-.")
    if (
        not host
        or len(host) > 253
        or any(character not in allowed_host_characters for character in host)
        or any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            for label in host.split(".")
        )
    ):
        raise FcaDataError("website_url host is invalid")
    path = parsed.path or "/"
    trailing_slash = path.endswith(("/", "/.", "/.."))
    segments: list[str] = []
    for segment in path.split("/"):
        if not segment or segment == ".":
            continue
        if segment == "..":
            if segments:
                segments.pop()
            continue
        segments.append(segment)
    canonical_path = "/" + "/".join(segments)
    if trailing_slash and canonical_path != "/":
        canonical_path += "/"
    return f"https://{host}{canonical_path}"


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
        website_url=canonicalize_website_url(raw.get("website_url")),
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
    if any(
        len(_canonical_record(record)) > FCA_MAX_CANONICAL_RECORD_CHARS
        for record in records
    ):
        raise FcaDataError("FCA record exceeded the immutable observation limit")
    frns = [record.frn for record in records]
    if len(frns) != len(set(frns)):
        raise FcaDataError("FCA payload contained a duplicate FRN")
    return records


def _canonical_record(record: FcaFirmRecord) -> str:
    return json.dumps(
        asdict(record), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )


def _prepare_ingest(
    records: Sequence[FcaFirmRecord],
    *,
    limit: int,
    now: datetime,
) -> tuple[tuple[FcaFirmRecord, ...], int, str]:
    if not 1 <= limit <= 100:
        raise ValueError("FCA ingestion limit must be between 1 and 100")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if not records:
        raise FcaDataError("FCA payload contained no firms")
    ordered = sorted(records, key=lambda item: item.frn)
    staged_count = min(limit, len(ordered))
    if staged_count == 1:
        selected = (ordered[0],)
    else:
        selected = tuple(
            ordered[round(index * (len(ordered) - 1) / (staged_count - 1))]
            for index in range(staged_count)
        )
    return selected, staged_count, now.astimezone(UTC).isoformat()


def _write_fca_records(
    conn: sqlite3.Connection,
    *,
    selected: Sequence[FcaFirmRecord],
    observed_at: str,
) -> tuple[int, int]:
    created_count = 0
    changed_count = 0
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
            INSERT INTO fca_observations (
                firm_id, observed_at, source_record_hash, canonical_record
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(firm_id, source_record_hash) DO NOTHING
            """,
            (firm_id, observed_at, record_hash, canonical),
        )
    return created_count, changed_count


def _ingest_fca_records_in_transaction(
    conn: sqlite3.Connection,
    records: Sequence[FcaFirmRecord],
    *,
    limit: int,
    now: datetime,
) -> IngestResult:
    if not conn.in_transaction:
        raise sqlite3.OperationalError("FCA ingestion transaction is not active")
    selected, staged_count, observed_at = _prepare_ingest(records, limit=limit, now=now)
    created_count, changed_count = _write_fca_records(
        conn, selected=selected, observed_at=observed_at
    )
    return IngestResult(len(records), staged_count, created_count, changed_count)


def ingest_fca_records(
    conn: sqlite3.Connection,
    records: Sequence[FcaFirmRecord],
    *,
    limit: int,
    now: datetime,
) -> IngestResult:
    if conn.in_transaction:
        raise sqlite3.OperationalError("FCA ingestion requires no active transaction")
    selected, staged_count, observed_at = _prepare_ingest(records, limit=limit, now=now)
    try:
        conn.execute("BEGIN IMMEDIATE")
        created_count, changed_count = _write_fca_records(
            conn, selected=selected, observed_at=observed_at
        )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return IngestResult(len(records), staged_count, created_count, changed_count)
