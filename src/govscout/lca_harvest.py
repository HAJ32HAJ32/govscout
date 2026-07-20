from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
from html.parser import HTMLParser
import sqlite3
from typing import Any, Callable, Protocol, Sequence
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


LCA_DIRECTORY_URL = "https://www.legionellacontrolassociation.co.uk/directory/"
LCA_COMPANY_HOST = "www.legionellacontrolassociation.co.uk"
LCA_MAX_RESPONSE_BYTES = 1_000_000
LCA_USER_AGENT = "GovScout/0.1 (+https://www.misegroup.co.uk/)"


class LcaDirectoryFormatError(ValueError):
    """Raised when the official directory no longer contains valid member cards."""


class LcaCandidateConflict(ValueError):
    """Raised when a known source URL returns different candidate evidence."""


@dataclass(frozen=True, slots=True)
class LcaDirectoryEntry:
    company_name: str
    source_location: str | None
    source_url: str


@dataclass(frozen=True, slots=True)
class HarvestResult:
    source_count: int
    staged_count: int
    created_count: int


class LcaDirectoryTransport(Protocol):
    def fetch_html(self, url: str) -> str: ...


class NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _open_without_redirects(request: Request, *, timeout: int):
    return build_opener(NoRedirectHandler()).open(request, timeout=timeout)


class UrlLcaDirectoryTransport:
    def __init__(self, opener: Callable[..., Any] | None = None):
        self._opener = opener or _open_without_redirects

    def fetch_html(self, url: str) -> str:
        if url != LCA_DIRECTORY_URL:
            raise ValueError("LCA transport only fetches the fixed official directory")
        request = Request(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "User-Agent": LCA_USER_AGENT,
            },
        )
        with self._opener(request, timeout=20) as response:
            if response.geturl() != LCA_DIRECTORY_URL:
                raise LcaDirectoryFormatError("LCA directory redirected unexpectedly")
            if response.headers.get_content_type() not in {
                "text/html",
                "application/xhtml+xml",
            }:
                raise LcaDirectoryFormatError("LCA directory did not return HTML")
            charset = response.headers.get_content_charset() or "utf-8"
            declared_length = response.headers.get("Content-Length")
            declared_bytes: int | None = None
            if declared_length is not None:
                try:
                    declared_bytes = int(declared_length)
                except ValueError as exc:
                    raise LcaDirectoryFormatError(
                        "LCA directory returned an invalid Content-Length"
                    ) from exc
                if not 0 <= declared_bytes <= LCA_MAX_RESPONSE_BYTES:
                    raise LcaDirectoryFormatError(
                        "LCA directory response exceeded 1 MB"
                    )
            payload = response.read(LCA_MAX_RESPONSE_BYTES + 1)
        if len(payload) > LCA_MAX_RESPONSE_BYTES:
            raise LcaDirectoryFormatError("LCA directory response exceeded 1 MB")
        if declared_bytes is not None and len(payload) != declared_bytes:
            raise LcaDirectoryFormatError("LCA directory response ended prematurely")
        try:
            return payload.decode(charset)
        except (LookupError, UnicodeDecodeError) as exc:
            raise LcaDirectoryFormatError("LCA directory response was not decodable") from exc


class _LcaDirectoryParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.entries: list[LcaDirectoryEntry] = []
        self.member_card_count = 0
        self.invalid_card_urls: list[str] = []
        self.directory_index_seen = False
        self.directory_index_closed = False
        self._directory_index_depth = 0
        self._href: str | None = None
        self._capture: str | None = None
        self._name_parts: list[str] = []
        self._location_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if tag == "div":
            if self._directory_index_depth:
                self._directory_index_depth += 1
            elif "lca-az-index" in classes:
                self.directory_index_seen = True
                self._directory_index_depth = 1
        if tag == "a" and "lca-az-link" in classes:
            self.member_card_count += 1
            self._href = attributes.get("href")
            self._name_parts = []
            self._location_parts = []
            return
        if self._href is not None and tag == "strong":
            self._capture = "name"
        elif self._href is not None and tag == "span":
            self._capture = "location"

    def handle_data(self, data: str) -> None:
        if self._capture == "name":
            self._name_parts.append(data)
        elif self._capture == "location":
            self._location_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "div" and self._directory_index_depth:
            self._directory_index_depth -= 1
            if self._directory_index_depth == 0:
                self.directory_index_closed = True
        if tag in {"strong", "span"}:
            self._capture = None
            return
        if tag != "a" or self._href is None:
            return
        company_name = " ".join("".join(self._name_parts).split())
        source_location = " ".join("".join(self._location_parts).split())
        if company_name and _is_lca_company_url(self._href):
            self.entries.append(
                LcaDirectoryEntry(
                    company_name=company_name,
                    source_location=source_location or None,
                    source_url=self._href,
                )
            )
        else:
            self.invalid_card_urls.append(self._href)
        self._href = None
        self._capture = None
        self._name_parts = []
        self._location_parts = []


def _is_lca_company_url(url: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname == LCA_COMPANY_HOST
        and parsed.port is None
        and parsed.username is None
        and parsed.password is None
        and parsed.path.startswith("/company/")
        and len(parsed.path) > len("/company/")
        and parsed.path.endswith("/")
        and not parsed.query
        and not parsed.fragment
    )


def parse_lca_directory(html: str) -> tuple[LcaDirectoryEntry, ...]:
    parser = _LcaDirectoryParser()
    parser.feed(html)
    parser.close()
    if not parser.entries:
        raise LcaDirectoryFormatError("LCA directory contained no valid member cards")
    if not parser.directory_index_seen or not parser.directory_index_closed:
        raise LcaDirectoryFormatError("LCA directory contained an incomplete member list")
    if parser.member_card_count != len(parser.entries):
        detail = parser.invalid_card_urls[0] if parser.invalid_card_urls else "unclosed card"
        raise LcaDirectoryFormatError(
            f"LCA directory contained an invalid member card: {detail!r}"
        )
    source_urls = [entry.source_url for entry in parser.entries]
    if len(source_urls) != len(set(source_urls)):
        raise LcaDirectoryFormatError("LCA directory contained a duplicate member URL")
    return tuple(parser.entries)


def _source_record_hash(entry: LcaDirectoryEntry) -> str:
    record = "\0".join(
        (entry.company_name, entry.source_location or "", entry.source_url)
    ).encode("utf-8")
    return hashlib.sha256(record).hexdigest()


def stage_lca_candidates(
    conn: sqlite3.Connection,
    entries: Sequence[LcaDirectoryEntry],
    *,
    limit: int,
    now: datetime,
) -> HarvestResult:
    if not 1 <= limit <= 50:
        raise ValueError("LCA harvest limit must be between 1 and 50")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if not entries:
        raise LcaDirectoryFormatError("LCA directory contained no valid member cards")
    if conn.in_transaction:
        raise sqlite3.OperationalError(
            "candidate staging requires a connection without an active transaction"
        )

    staged_count = min(limit, len(entries))
    selected = tuple(
        entries[index * len(entries) // staged_count] for index in range(staged_count)
    )
    observed_at = now.astimezone(UTC).isoformat()
    created_count = 0
    transaction_started = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        transaction_started = True
        for entry in selected:
            existing = conn.execute(
                """
                SELECT source_record_hash, last_seen_at FROM candidates
                WHERE source_register = 'LCA member directory' AND source_url = ?
                """,
                (entry.source_url,),
            ).fetchone()
            record_hash = _source_record_hash(entry)
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO candidates (
                        source_register, source_url, company_name, source_location,
                        source_record_hash, discovered_at, last_seen_at
                    ) VALUES ('LCA member directory', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        entry.source_url,
                        entry.company_name,
                        entry.source_location,
                        record_hash,
                        observed_at,
                        observed_at,
                    ),
                )
                created_count += 1
            elif existing["source_record_hash"] != record_hash:
                raise LcaCandidateConflict(
                    f"LCA source record changed for {entry.source_url}"
                )
            elif existing["last_seen_at"] > observed_at:
                raise LcaCandidateConflict(
                    f"LCA observation is older than last observation for {entry.source_url}"
                )
            else:
                conn.execute(
                    """
                    UPDATE candidates SET last_seen_at = ?
                    WHERE source_register = 'LCA member directory' AND source_url = ?
                    """,
                    (observed_at, entry.source_url),
                )
        conn.execute("COMMIT")
    except Exception:
        if transaction_started and conn.in_transaction:
            conn.execute("ROLLBACK")
        raise

    return HarvestResult(
        source_count=len(entries),
        staged_count=staged_count,
        created_count=created_count,
    )


def harvest_lca(
    conn: sqlite3.Connection,
    transport: LcaDirectoryTransport,
    *,
    limit: int,
    now: datetime,
) -> HarvestResult:
    if not 1 <= limit <= 50:
        raise ValueError("LCA harvest limit must be between 1 and 50")
    html = transport.fetch_html(LCA_DIRECTORY_URL)
    entries = parse_lca_directory(html)
    return stage_lca_candidates(conn, entries, limit=limit, now=now)
