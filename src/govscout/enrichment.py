from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
from html.parser import HTMLParser
import http.client
import ipaddress
import socket
import sqlite3
import ssl
from typing import Any, Callable, Protocol
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request

from govscout.fca_discovery import FcaDataError, canonicalize_website_url
from govscout.quality import company_verification_is_current


SITE_MAX_RESPONSE_BYTES = 512_000
SITE_TIMEOUT_SECONDS = 15
SITE_USER_AGENT = "GovScout/0.1 (+https://www.misegroup.co.uk/)"
_FCA_IDENTITY_FIELDS = (
    "frn", "firm_name", "fca_status", "firm_type", "is_active", "source_url",
    "website_url", "source_location", "company_number", "lead_id", "source_record_hash",
    "first_seen_at", "last_seen_at",
)


class SiteFetchError(ValueError):
    """A site could not be safely treated as enrichment evidence."""


@dataclass(frozen=True, slots=True)
class SitePage:
    url: str
    final_url: str
    html: str
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class EnrichmentResult:
    run_id: int
    score: int
    temperature: str


@dataclass(frozen=True, slots=True)
class _Evidence:
    signal_group: str
    code: str
    state: str
    weight: int
    source_url: str | None
    excerpt: str | None
    content_hash: str


class SiteTransport(Protocol):
    def fetch_html(self, url: str) -> SitePage: ...


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *, pinned_ip: str, server_hostname: str, timeout: int):
        super().__init__(
            server_hostname,
            port=443,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        sock = socket.create_connection(
            (self._pinned_ip, self.port),
            self.timeout,
            self.source_address,
        )
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _PinnedResponse:
    def __init__(
        self,
        *,
        connection: _PinnedHTTPSConnection,
        response: http.client.HTTPResponse,
        url: str,
    ):
        self._connection = connection
        self._response = response
        self._url = url
        self.headers = response.headers
        self.status = response.status

    def __enter__(self):
        return self

    def __exit__(self, *_args: Any) -> None:
        self._response.close()
        self._connection.close()

    def geturl(self) -> str:
        return self._url

    def read(self, size: int) -> bytes:
        return self._response.read(size)


def _default_open(
    request: Request,
    *,
    timeout: int,
    pinned_ip: str,
    server_hostname: str,
):
    connection = _PinnedHTTPSConnection(
        pinned_ip=pinned_ip,
        server_hostname=server_hostname,
        timeout=timeout,
    )
    try:
        connection.request("GET", request.selector, headers=dict(request.header_items()))
        response = connection.getresponse()
    except Exception:
        connection.close()
        raise
    return _PinnedResponse(connection=connection, response=response, url=request.full_url)


class UrlSiteTransport:
    """HTTPS-only, public-address, no-redirect, bounded website transport."""

    def __init__(
        self,
        *,
        opener: Callable[..., Any] | None = None,
        resolver: Callable[..., Any] | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ):
        self._opener = opener or _default_open
        self._resolver = resolver or socket.getaddrinfo
        self._clock = now_provider or (lambda: datetime.now(UTC))

    def fetch_html(self, url: str) -> SitePage:
        try:
            canonical_url = canonicalize_website_url(url)
        except FcaDataError as exc:
            raise SiteFetchError("UNSAFE_URL") from exc
        if canonical_url is None or canonical_url != url:
            raise SiteFetchError("UNSAFE_URL")
        parsed = urlsplit(url)
        if not (
            parsed.scheme == "https"
            and parsed.hostname
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
        ):
            raise SiteFetchError("UNSAFE_URL")
        try:
            addresses = self._resolver(parsed.hostname, 443, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise SiteFetchError("DNS_FAILED") from exc
        if not addresses:
            raise SiteFetchError("DNS_FAILED")
        for address in addresses:
            if not ipaddress.ip_address(address[4][0]).is_global:
                raise SiteFetchError("NON_PUBLIC_ADDRESS")
        request = Request(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Host": parsed.hostname,
                "User-Agent": SITE_USER_AGENT,
            },
        )
        try:
            with self._opener(
                request,
                timeout=SITE_TIMEOUT_SECONDS,
                pinned_ip=addresses[0][4][0],
                server_hostname=parsed.hostname,
            ) as response:
                status = getattr(response, "status", 200)
                if 300 <= status < 400:
                    raise SiteFetchError("REDIRECTED")
                if status == 404:
                    raise SiteFetchError("NOT_FOUND")
                if not 200 <= status < 300:
                    raise SiteFetchError("FETCH_FAILED")
                if response.geturl() != url:
                    raise SiteFetchError("REDIRECTED")
                if response.headers.get_content_type() not in {
                    "text/html",
                    "application/xhtml+xml",
                }:
                    raise SiteFetchError("UNSUPPORTED_CONTENT_TYPE")
                declared = response.headers.get("Content-Length")
                if declared is not None:
                    try:
                        declared_size = int(declared)
                    except ValueError as exc:
                        raise SiteFetchError("INVALID_CONTENT_LENGTH") from exc
                    if not 0 <= declared_size <= SITE_MAX_RESPONSE_BYTES:
                        raise SiteFetchError("RESPONSE_TOO_LARGE")
                payload = response.read(SITE_MAX_RESPONSE_BYTES + 1)
                charset = response.headers.get_content_charset() or "utf-8"
        except SiteFetchError:
            raise
        except HTTPError as exc:
            if exc.code == 404:
                raise SiteFetchError("NOT_FOUND") from exc
            raise SiteFetchError("FETCH_FAILED") from exc
        except OSError as exc:
            raise SiteFetchError("FETCH_FAILED") from exc
        if len(payload) > SITE_MAX_RESPONSE_BYTES:
            raise SiteFetchError("RESPONSE_TOO_LARGE")
        try:
            html = payload.decode(charset)
        except (LookupError, UnicodeDecodeError) as exc:
            raise SiteFetchError("UNDECODABLE_CONTENT") from exc
        return SitePage(url=url, final_url=url, html=html, fetched_at=self._clock())


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored = 0

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._ignored += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._ignored:
            self._ignored -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored:
            self.parts.append(data)


def _plain_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return " ".join(" ".join(parser.parts).split())


def _excerpt(text: str, keywords: tuple[str, ...]) -> str | None:
    lowered = text.casefold()
    positions = [lowered.find(word) for word in keywords if lowered.find(word) >= 0]
    if not positions:
        return None
    start = max(0, min(positions) - 80)
    return text[start : start + 300]


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _assert_firm_snapshot_current(
    conn: sqlite3.Connection, *, firm_id: int, snapshot: sqlite3.Row
) -> None:
    current = conn.execute("SELECT * FROM fca_firms WHERE id = ?", (firm_id,)).fetchone()
    if current is None or any(current[field] != snapshot[field] for field in _FCA_IDENTITY_FIELDS):
        raise SiteFetchError("FCA_IDENTITY_CHANGED")


def run_enrichment(
    conn: sqlite3.Connection,
    *,
    firm_id: int,
    transport: SiteTransport,
    now: datetime,
) -> EnrichmentResult:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    firm = conn.execute(
        """
        SELECT *
        FROM fca_firms WHERE id = ?
        """,
        (firm_id,),
    ).fetchone()
    if firm is None:
        raise KeyError(firm_id)
    if not company_verification_is_current(conn, firm_id=firm_id, now=now):
        raise SiteFetchError("COMPANIES_HOUSE_VERIFICATION_REQUIRED")
    website = firm["website_url"]
    if website is None:
        raise SiteFetchError("WEBSITE_MISSING")
    parsed = urlsplit(website)
    origin = f"https://{parsed.netloc}/"
    targets = {
        "home": website,
        "privacy": urljoin(origin, "privacy"),
        "careers": urljoin(origin, "careers"),
        "policy": urljoin(origin, "ai-policy"),
    }
    pages: dict[str, SitePage] = {}
    failures: dict[str, str] = {}
    for key, url in targets.items():
        try:
            page = transport.fetch_html(url)
        except SiteFetchError as exc:
            failures[key] = str(exc)
            continue
        if page.url != url or page.final_url != url:
            failures[key] = "REDIRECTED"
            continue
        if page.fetched_at.tzinfo is None or page.fetched_at.utcoffset() is None:
            failures[key] = "INVALID_TIMESTAMP"
            continue
        pages[key] = page
    timestamp = now.astimezone(UTC).isoformat()
    if "home" not in pages:
        failure = failures.get("home", "FETCH_FAILED")
        try:
            conn.execute("BEGIN IMMEDIATE")
            _assert_firm_snapshot_current(conn, firm_id=firm_id, snapshot=firm)
            conn.execute(
                """
                INSERT INTO enrichment_runs (
                    firm_id, state, started_at, completed_at, website_url,
                    input_hash, failure_code
                ) VALUES (?, 'failed', ?, ?, ?, ?, ?)
                """,
                (firm_id, timestamp, timestamp, website, firm["source_record_hash"], failure),
            )
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        raise SiteFetchError(failure)

    texts = {key: _plain_text(page.html) for key, page in pages.items()}
    evidence: list[_Evidence] = [
        _Evidence(
            "accountability",
            "FCA_REGULATED",
            "present",
            40,
            firm["source_url"],
            f"FCA status: {firm['fca_status']}",
            firm["source_record_hash"],
        )
    ]
    for key in ("privacy", "careers", "policy"):
        if key in failures:
            failure = failures[key]
            evidence.append(
                _Evidence(
                    "site_health",
                    f"{key.upper()}_SCAN_STATUS",
                    "unknown",
                    0,
                    targets[key],
                    None,
                    _hash(f"{key}:{failure}"),
                )
            )
    ai_keywords = ("ai-powered", "artificial intelligence", "chatgpt", "copilot", "chatbot")
    ai_sources = [
        (pages[key].url, excerpt)
        for key, text in texts.items()
        if (excerpt := _excerpt(text, ai_keywords)) is not None
    ]
    ai_visible = bool(ai_sources)
    if ai_visible:
        url, excerpt = ai_sources[0]
        evidence.append(_Evidence("ai_exposure", "AI_VISIBLE", "present", 30, url, excerpt, _hash(excerpt)))
    else:
        evidence.append(_Evidence("ai_exposure", "AI_VISIBLE", "absent", 0, website, None, _hash("AI_VISIBLE:absent")))

    privacy_text = texts.get("privacy")
    privacy_silent = False
    if privacy_text is None:
        evidence.append(_Evidence("governance_gap", "PRIVACY_AI_STATUS", "unknown", 0, targets["privacy"], None, _hash(failures.get("privacy", "unknown"))))
    else:
        privacy_terms = ("automated decision", "artificial intelligence", " ai ")
        privacy_silent = not any(term in f" {privacy_text.casefold()} " for term in privacy_terms)
        if privacy_silent:
            excerpt = (privacy_text or "Privacy page contains no readable text")[:300]
            evidence.append(_Evidence("governance_gap", "PRIVACY_SILENT_ON_AI", "present", 15, pages["privacy"].url, excerpt, _hash(privacy_text)))
        else:
            evidence.append(_Evidence("governance_gap", "PRIVACY_SILENT_ON_AI", "absent", 0, pages["privacy"].url, None, _hash(privacy_text)))

    policy_missing = "policy" not in pages and failures.get("policy") == "NOT_FOUND"
    if policy_missing:
        evidence.append(_Evidence("governance_gap", "AI_POLICY_NOT_FOUND", "present", 15, targets["policy"], "Page not found (NOT_FOUND)", _hash("NOT_FOUND")))
    elif "policy" not in pages:
        evidence.append(_Evidence("governance_gap", "AI_POLICY_STATUS", "unknown", 0, targets["policy"], None, _hash(failures.get("policy", "unknown"))))
    else:
        evidence.append(_Evidence("governance_gap", "AI_POLICY_NOT_FOUND", "absent", 0, pages["policy"].url, None, _hash(texts["policy"])))

    gap_score = 30 if privacy_silent and policy_missing else (15 if privacy_silent else 0)
    score = min(100, 40 + (30 if ai_visible else 0) + gap_score)
    temperature = "HOT" if score >= 75 else "WARM" if score >= 55 else "COOL"
    page_hash = _hash("\0".join(page.html for page in pages.values()))
    try:
        conn.execute("BEGIN IMMEDIATE")
        _assert_firm_snapshot_current(conn, firm_id=firm_id, snapshot=firm)
        run_id = conn.execute(
            """
            INSERT INTO enrichment_runs (
                firm_id, state, started_at, website_url, input_hash
            ) VALUES (?, 'running', ?, ?, ?)
            """,
            (firm_id, timestamp, website, firm["source_record_hash"]),
        ).lastrowid
        for item in evidence:
            conn.execute(
                """
                INSERT INTO evidence_items (
                    run_id, signal_group, code, evidence_state, weight,
                    source_url, excerpt, observed_at, content_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, item.signal_group, item.code, item.state, item.weight, item.source_url, item.excerpt, timestamp, item.content_hash),
            )
        conn.execute(
            """
            UPDATE enrichment_runs
            SET state = 'complete', completed_at = ?, final_url = ?, page_hash = ?,
                score = ?, temperature = ?
            WHERE id = ? AND state = 'running'
            """,
            (timestamp, website, page_hash, score, temperature, run_id),
        )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return EnrichmentResult(run_id=int(run_id), score=score, temperature=temperature)
