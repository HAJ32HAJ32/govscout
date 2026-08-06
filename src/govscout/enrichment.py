from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
import hashlib
from html.parser import HTMLParser
import http.client
import ipaddress
import re
import socket
import sqlite3
import ssl
from typing import Any, Callable, Protocol
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit

from govscout.website_research import (
    WebsiteResearchConflict,
    load_current_reprocessing_input,
)
from urllib.request import Request

from govscout.fca_discovery import FcaDataError, canonicalize_website_url
from govscout.quality import company_verification_is_current


SITE_MAX_RESPONSE_BYTES = 512_000
SITE_TIMEOUT_SECONDS = 15
SITE_USER_AGENT = "GovScout/0.1 (+https://www.misegroup.co.uk/)"
SITE_MAX_REDIRECTS = 3
_FCA_IDENTITY_FIELDS = (
    "frn", "firm_name", "fca_status", "firm_type", "is_active", "source_url",
    "website_url", "source_location", "company_number", "lead_id", "source_record_hash",
    "first_seen_at", "last_seen_at",
)
_HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml")
_XML_CONTENT_TYPES = ("application/xml", "text/xml")
SITEMAP_MAX_URLS = 200
_ESTABLISHED_COMPANY_MIN_DAYS = 3 * 365
_SITEMAP_LOC_PATTERN = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)

# One guessed path each for the original three keys (unchanged, to keep existing
# behaviour/tests stable); terms and cookies get two guesses since they weren't
# discoverable at all before.
_GUESSED_PATHS = {
    "privacy": ("privacy",),
    "careers": ("careers",),
    "policy": ("ai-policy",),
    "terms": ("terms", "terms-and-conditions"),
    "cookies": ("cookies", "cookie-policy"),
}

# Used to match sitemap.xml <loc> URL paths (not link text, so single hyphenated
# tokens rather than the phrase-style terms used for scanning homepage link text).
_SITEMAP_PATH_KEYWORDS = {
    "privacy": ("privacy",),
    "careers": ("career", "jobs"),
    "policy": ("policy", "responsible-ai", "ai-governance"),
    "terms": ("terms",),
    "cookies": ("cookie",),
}


class SiteFetchError(ValueError):
    """A site could not be safely treated as enrichment evidence."""

    def __init__(self, code: str, *, final_url: str | None = None):
        super().__init__(code)
        self.code = code
        self.final_url = final_url


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

    def fetch_sitemap(self, url: str) -> list[str]:
        """Optional: return <loc> URLs from a sitemap.xml. Implementations that don't
        support it may omit this method entirely -- callers treat it as unavailable
        via getattr, not a hard requirement."""
        ...


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
    """HTTPS-only, public-address, safely redirected, bounded site transport."""

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

    def _fetch_bytes(
        self, url: str, *, accepted_content_types: tuple[str, ...]
    ) -> tuple[bytes, str, str]:
        """Core HTTPS-only, public-address, safely redirected, bounded fetch. Shared by
        fetch_html and fetch_sitemap -- only the accepted content type differs."""
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
        origin = (parsed.scheme, parsed.hostname, parsed.port)
        for redirect_count in range(SITE_MAX_REDIRECTS + 1):
            parsed = urlsplit(url)
            if (parsed.scheme, parsed.hostname, parsed.port) != origin:
                raise SiteFetchError("REDIRECTED")
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
                    "Accept": ",".join(accepted_content_types),
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
                        if redirect_count == SITE_MAX_REDIRECTS:
                            raise SiteFetchError("REDIRECTED")
                        location = response.headers.get("Location")
                        if not location:
                            raise SiteFetchError("REDIRECTED")
                        redirected_url = urljoin(url, location)
                        try:
                            redirected_canonical = canonicalize_website_url(redirected_url)
                        except FcaDataError as exc:
                            raise SiteFetchError("REDIRECTED") from exc
                        redirected = urlsplit(redirected_url)
                        if (
                            redirected_canonical != redirected_url
                            or (redirected.scheme, redirected.hostname, redirected.port) != origin
                        ):
                            raise SiteFetchError("REDIRECTED")
                        url = redirected_url
                        continue
                    if status == 404:
                        raise SiteFetchError("NOT_FOUND", final_url=url)
                    if not 200 <= status < 300:
                        raise SiteFetchError("FETCH_FAILED")
                    if response.geturl() != url:
                        raise SiteFetchError("REDIRECTED")
                    if response.headers.get_content_type() not in accepted_content_types:
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
                    break
            except SiteFetchError:
                raise
            except HTTPError as exc:
                if exc.code == 404:
                    raise SiteFetchError("NOT_FOUND", final_url=url) from exc
                raise SiteFetchError("FETCH_FAILED") from exc
            except OSError as exc:
                raise SiteFetchError("FETCH_FAILED") from exc
            if 200 <= status < 300:
                break
        if len(payload) > SITE_MAX_RESPONSE_BYTES:
            raise SiteFetchError("RESPONSE_TOO_LARGE")
        return payload, url, charset

    def fetch_html(self, url: str) -> SitePage:
        original_url = url
        payload, final_url, charset = self._fetch_bytes(
            url, accepted_content_types=_HTML_CONTENT_TYPES
        )
        try:
            html = payload.decode(charset)
        except (LookupError, UnicodeDecodeError) as exc:
            raise SiteFetchError("UNDECODABLE_CONTENT") from exc
        return SitePage(
            url=original_url,
            final_url=final_url,
            html=html,
            fetched_at=self._clock(),
        )

    def fetch_sitemap(self, url: str) -> list[str]:
        payload, _final_url, charset = self._fetch_bytes(
            url, accepted_content_types=_XML_CONTENT_TYPES
        )
        try:
            text = payload.decode(charset)
        except (LookupError, UnicodeDecodeError) as exc:
            raise SiteFetchError("UNDECODABLE_CONTENT") from exc
        return _SITEMAP_LOC_PATTERN.findall(text)[:SITEMAP_MAX_URLS]


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


class _LinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._href = next((value for name, value in attrs if name == "href"), None)
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join(self._parts)))
            self._href = None
            self._parts = []


class _ScriptSourceExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            src = next((value for name, value in attrs if name == "src" and value), None)
            if src:
                self.sources.append(src)


# Common third-party analytics/chat-widget script hosts -- a first, deliberately small
# "digital tooling present" signal, not an exhaustive vendor list.
_TECH_TOOLING_HOSTS = (
    "google-analytics.com",
    "googletagmanager.com",
    "hotjar.com",
    "segment.com",
    "intercom.io",
    "drift.com",
    "tawk.to",
    "crisp.chat",
    "zendesk.com",
    "hubspot.com",
    "livechatinc.com",
)


def _detect_tech_tooling(html: str) -> str | None:
    """Return the first known analytics/chat-widget script hostname found, if any."""
    parser = _ScriptSourceExtractor()
    parser.feed(html)
    parser.close()
    for src in parser.sources:
        hostname = (urlsplit(src).hostname or "").casefold()
        if any(hostname == host or hostname.endswith(f".{host}") for host in _TECH_TOOLING_HOSTS):
            return hostname
    return None


def _discover_evidence_links(html: str, *, base_url: str) -> dict[str, str]:
    parser = _LinkExtractor()
    parser.feed(html)
    parser.close()
    base = urlsplit(base_url)
    keywords = {
        "privacy": ("privacy", "data protection"),
        "careers": ("career", "jobs", "vacancies", "join us"),
        "policy": ("ai policy", "responsible ai", "ai governance", "artificial intelligence"),
        "terms": ("terms", "terms and conditions", "terms of service", "t&cs"),
        "cookies": ("cookie", "cookies policy"),
    }
    discovered: dict[str, str] = {}
    for href, label in parser.links:
        candidate = urljoin(base_url, href)
        haystack = f"{href} {label}".casefold()
        key = next(
            (name for name, terms in keywords.items() if any(term in haystack for term in terms)),
            None,
        )
        if key is None or key in discovered:
            continue
        try:
            canonical = canonicalize_website_url(candidate)
        except FcaDataError:
            continue
        parsed = urlsplit(candidate)
        if canonical == candidate and (
            parsed.scheme,
            parsed.hostname,
            parsed.port,
        ) == (base.scheme, base.hostname, base.port):
            discovered[key] = candidate
    return discovered


def _excerpt(text: str, keywords: tuple[str, ...]) -> str | None:
    lowered = text.casefold()
    positions = [lowered.find(word) for word in keywords if lowered.find(word) >= 0]
    if not positions:
        return None
    start = max(0, min(positions) - 80)
    return text[start : start + 300]


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _same_origin_canonical_url(candidate_url: str, *, requested_url: str) -> bool:
    try:
        canonical = canonicalize_website_url(candidate_url)
    except FcaDataError:
        return False
    requested = urlsplit(requested_url)
    candidate = urlsplit(candidate_url)
    return canonical == candidate_url and (
        requested.scheme,
        requested.hostname,
        requested.port,
    ) == (candidate.scheme, candidate.hostname, candidate.port)


def _page_result_is_safe(page: SitePage, *, requested_url: str) -> bool:
    return page.url == requested_url and _same_origin_canonical_url(
        page.final_url,
        requested_url=requested_url,
    )


def _attempt_candidate(
    transport: SiteTransport, url: str, *, home_page: SitePage
) -> tuple[SitePage | None, str | None, str | None]:
    """Try fetching one candidate URL as an auxiliary evidence page.

    Returns (page, None, None) on success, or (None, failure_code, failure_url) on
    failure. Mirrors the single-shot validation previously inlined in run_enrichment:
    same-origin/canonical redirect safety, a valid timestamp, and rejecting content
    that's identical to the homepage (a generic "everything redirects here" site).
    """
    try:
        page = transport.fetch_html(url)
    except SiteFetchError as exc:
        failure_url = (
            exc.final_url
            if exc.final_url and _same_origin_canonical_url(exc.final_url, requested_url=url)
            else url
        )
        return None, str(exc), failure_url
    if not _page_result_is_safe(page, requested_url=url):
        return None, "REDIRECTED", url
    if page.fetched_at.tzinfo is None or page.fetched_at.utcoffset() is None:
        return None, "INVALID_TIMESTAMP", url
    if _plain_text(page.html).casefold() == _plain_text(home_page.html).casefold():
        return None, "DUPLICATE_CONTENT", page.final_url
    if page.final_url != home_page.final_url:
        return page, None, None
    return None, "REDIRECTED", page.final_url


def _assert_firm_snapshot_current(
    conn: sqlite3.Connection, *, firm_id: int, snapshot: sqlite3.Row
) -> None:
    current = conn.execute("SELECT * FROM fca_firms WHERE id = ?", (firm_id,)).fetchone()
    if current is None or any(current[field] != snapshot[field] for field in _FCA_IDENTITY_FIELDS):
        raise SiteFetchError("FCA_IDENTITY_CHANGED")


def _assert_verification_current(
    conn: sqlite3.Connection,
    *,
    firm_id: int,
    verification_attempt_id: int,
    now: datetime,
) -> None:
    latest = conn.execute(
        """
        SELECT id FROM company_verification_attempts
        WHERE firm_id = ? ORDER BY id DESC LIMIT 1
        """,
        (firm_id,),
    ).fetchone()
    if (
        latest is None
        or int(latest["id"]) != verification_attempt_id
        or not company_verification_is_current(conn, firm_id=firm_id, now=now)
    ):
        raise SiteFetchError("COMPANIES_HOUSE_VERIFICATION_CHANGED")


def _assert_reprocessing_current(
    conn: sqlite3.Connection,
    *,
    firm_id: int,
    website_url: str,
    website_evidence_event_id: int | None,
    company_verification_attempt_id: int | None,
    processing_input_hash: str,
    reprocessing_job_id: int | None,
    source_record_hash: str,
    now: datetime,
) -> None:
    if website_evidence_event_id is None:
        if (
            company_verification_attempt_id is not None
            or reprocessing_job_id is not None
            or processing_input_hash != source_record_hash
        ):
            raise SiteFetchError("WEBSITE_EVIDENCE_CHANGED")
        return
    if (
        company_verification_attempt_id is None
        or reprocessing_job_id is None
    ):
        raise SiteFetchError("WEBSITE_EVIDENCE_CHANGED")
    try:
        current = load_current_reprocessing_input(
            conn, job_id=reprocessing_job_id, now=now
        )
    except (KeyError, ValueError, WebsiteResearchConflict) as exc:
        raise SiteFetchError("WEBSITE_EVIDENCE_CHANGED") from exc
    if (
        current.firm_id != firm_id
        or current.website_url != website_url
        or current.website_evidence_event_id != website_evidence_event_id
        or current.company_verification_attempt_id != company_verification_attempt_id
        or current.input_hash != processing_input_hash
    ):
        raise SiteFetchError("WEBSITE_EVIDENCE_CHANGED")


def run_enrichment(
    conn: sqlite3.Connection,
    *,
    firm_id: int,
    transport: SiteTransport,
    website_url: str | None = None,
    website_evidence_event_id: int | None = None,
    company_verification_attempt_id: int | None = None,
    processing_input_hash: str | None = None,
    reprocessing_job_id: int | None = None,
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
    verification = conn.execute(
        """
        SELECT id, incorporation_date FROM company_verification_attempts
        WHERE firm_id = ? ORDER BY id DESC LIMIT 1
        """,
        (firm_id,),
    ).fetchone()
    if verification is None:
        raise SiteFetchError("COMPANIES_HOUSE_VERIFICATION_REQUIRED")
    verification_attempt_id = int(verification["id"])
    incorporation_date = verification["incorporation_date"]
    if website_evidence_event_id is None and website_url is not None:
        raise SiteFetchError("WEBSITE_EVIDENCE_CHANGED")
    website = (
        website_url
        if website_evidence_event_id is not None
        else firm["website_url"]
    )
    if website is None:
        raise SiteFetchError("WEBSITE_MISSING")
    effective_input_hash = processing_input_hash or firm["source_record_hash"]
    if (
        company_verification_attempt_id is not None
        and company_verification_attempt_id != verification_attempt_id
    ):
        raise SiteFetchError("COMPANIES_HOUSE_VERIFICATION_CHANGED")
    _assert_reprocessing_current(
        conn,
        firm_id=firm_id,
        website_url=website,
        website_evidence_event_id=website_evidence_event_id,
        company_verification_attempt_id=company_verification_attempt_id,
        processing_input_hash=effective_input_hash,
        reprocessing_job_id=reprocessing_job_id,
        source_record_hash=firm["source_record_hash"],
        now=now,
    )
    parsed = urlsplit(website)
    origin = f"https://{parsed.netloc}/"
    pages: dict[str, SitePage] = {}
    failures: dict[str, str] = {}
    failure_urls: dict[str, str] = {}
    try:
        home = transport.fetch_html(website)
    except SiteFetchError as exc:
        failures["home"] = str(exc)
        failure_urls["home"] = (
            exc.final_url
            if exc.final_url
            and _same_origin_canonical_url(exc.final_url, requested_url=website)
            else website
        )
    else:
        if not _page_result_is_safe(home, requested_url=website):
            failures["home"] = "REDIRECTED"
        elif home.fetched_at.tzinfo is None or home.fetched_at.utcoffset() is None:
            failures["home"] = "INVALID_TIMESTAMP"
        else:
            pages["home"] = home
    discovered = (
        _discover_evidence_links(
            pages["home"].html,
            base_url=pages["home"].final_url,
        )
        if "home" in pages
        else {}
    )
    targets: dict[str, str] = {"home": website}
    sitemap_urls: list[str] | None = None

    def _get_sitemap_urls() -> list[str]:
        nonlocal sitemap_urls
        if sitemap_urls is None:
            fetch_sitemap = getattr(transport, "fetch_sitemap", None)
            if fetch_sitemap is None:
                sitemap_urls = []
            else:
                try:
                    sitemap_urls = [
                        candidate
                        for candidate in fetch_sitemap(urljoin(origin, "sitemap.xml"))
                        if _same_origin_canonical_url(candidate, requested_url=website)
                    ]
                except SiteFetchError:
                    sitemap_urls = []
        return sitemap_urls

    # Aux-page results are discarded entirely below if home itself failed to fetch
    # (raises before anything is persisted), so there's no point attempting them.
    for key in ("privacy", "careers", "policy", "terms", "cookies") if "home" in pages else ():
        candidates = (
            [discovered[key]]
            if key in discovered
            else [urljoin(origin, path) for path in _GUESSED_PATHS[key]]
        )
        targets[key] = candidates[0]
        for candidate_url in candidates:
            page, failure_code, failure_url = _attempt_candidate(
                transport, candidate_url, home_page=pages["home"]
            )
            if page is not None:
                pages[key] = page
                failures.pop(key, None)
                failure_urls.pop(key, None)
                break
            failures[key] = failure_code
            failure_urls[key] = failure_url or candidate_url
            if failure_code != "NOT_FOUND":
                break
        else:
            # Every guessed path came back NOT_FOUND -- try one sitemap-matched URL
            # as a last resort before giving up on this key.
            matched = next(
                (
                    candidate
                    for candidate in _get_sitemap_urls()
                    if any(
                        term in urlsplit(candidate).path.casefold()
                        for term in _SITEMAP_PATH_KEYWORDS[key]
                    )
                ),
                None,
            )
            if matched is not None:
                page, failure_code, failure_url = _attempt_candidate(
                    transport, matched, home_page=pages["home"]
                )
                if page is not None:
                    pages[key] = page
                    failures.pop(key, None)
                    failure_urls.pop(key, None)
                else:
                    failures[key] = failure_code
                    failure_urls[key] = failure_url or matched
    timestamp = now.astimezone(UTC).isoformat()
    if "home" not in pages:
        failure = failures.get("home", "FETCH_FAILED")
        try:
            conn.execute("BEGIN IMMEDIATE")
            _assert_firm_snapshot_current(conn, firm_id=firm_id, snapshot=firm)
            _assert_verification_current(
                conn,
                firm_id=firm_id,
                verification_attempt_id=verification_attempt_id,
                now=now,
            )
            _assert_reprocessing_current(
                conn,
                firm_id=firm_id,
                website_url=website,
                website_evidence_event_id=website_evidence_event_id,
                company_verification_attempt_id=company_verification_attempt_id,
                processing_input_hash=effective_input_hash,
                reprocessing_job_id=reprocessing_job_id,
                source_record_hash=firm["source_record_hash"],
                now=now,
            )
            conn.execute(
                """
                INSERT INTO enrichment_runs (
                    firm_id, state, started_at, completed_at, website_url,
                    input_hash, failure_code, website_evidence_event_id,
                    company_verification_attempt_id
                ) VALUES (?, 'failed', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    firm_id, timestamp, timestamp, website,
                    effective_input_hash, failure,
                    website_evidence_event_id, company_verification_attempt_id,
                ),
            )
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        raise SiteFetchError(failure)

    texts = {key: _plain_text(page.html) for key, page in pages.items()}
    incorporated_on: date | None = None
    if incorporation_date:
        try:
            incorporated_on = datetime.strptime(incorporation_date, "%Y-%m-%d").date()
        except ValueError:
            incorporated_on = None
    established = (
        incorporated_on is not None
        and (now.astimezone(UTC).date() - incorporated_on).days >= _ESTABLISHED_COMPANY_MIN_DAYS
    )
    evidence: list[_Evidence] = [
        _Evidence(
            "accountability",
            "FCA_REGULATED",
            "present",
            40,
            firm["source_url"],
            f"FCA status: {firm['fca_status']}",
            firm["source_record_hash"],
        ),
        _Evidence(
            "accountability",
            "ESTABLISHED_COMPANY",
            "present" if established else "absent",
            10 if established else 0,
            firm["source_url"],
            f"Incorporated {incorporation_date}" if established else None,
            _hash(f"ESTABLISHED_COMPANY:{incorporation_date or 'unknown'}"),
        ),
    ]
    for key in ("privacy", "careers", "policy", "terms", "cookies"):
        if key in failures:
            failure = failures[key]
            if failure == "NOT_FOUND":
                evidence.append(
                    _Evidence(
                        "site_health",
                        f"{key.upper()}_URL_NOT_FOUND",
                        "present",
                        0,
                        failure_urls.get(key, targets[key]),
                        "Requested URL returned NOT_FOUND",
                        _hash(f"{key}:NOT_FOUND"),
                    )
                )
                continue
            evidence.append(
                _Evidence(
                    "site_health",
                    f"{key.upper()}_SCAN_STATUS",
                    "unknown",
                    0,
                    failure_urls.get(key, targets[key]),
                    None,
                    _hash(f"{key}:{failure}"),
                )
            )
    ai_keywords = ("ai-powered", "artificial intelligence", "chatgpt", "copilot", "chatbot")
    ai_sources = [
        (pages[key].final_url, excerpt)
        for key, text in texts.items()
        if (excerpt := _excerpt(text, ai_keywords)) is not None
    ]
    ai_visible = bool(ai_sources)
    if ai_visible:
        url, excerpt = ai_sources[0]
        evidence.append(_Evidence("ai_exposure", "AI_VISIBLE", "present", 30, url, excerpt, _hash(excerpt)))
    else:
        evidence.append(_Evidence("ai_exposure", "AI_VISIBLE", "absent", 0, website, None, _hash("AI_VISIBLE:absent")))

    tech_match = _detect_tech_tooling(pages["home"].html)
    evidence.append(
        _Evidence(
            "site_health",
            "TECH_TOOLING_DETECTED",
            "present" if tech_match else "absent",
            5 if tech_match else 0,
            pages["home"].final_url,
            f"Script loaded from {tech_match}" if tech_match else None,
            _hash(f"TECH_TOOLING_DETECTED:{tech_match or 'none'}"),
        )
    )

    privacy_text = texts.get("privacy")
    privacy_silent = False
    if privacy_text is None:
        if failures.get("privacy") != "NOT_FOUND":
            evidence.append(_Evidence("governance_gap", "PRIVACY_AI_STATUS", "unknown", 0, failure_urls.get("privacy", targets["privacy"]), None, _hash(failures.get("privacy", "unknown"))))
    else:
        privacy_terms = ("automated decision", "artificial intelligence", " ai ")
        privacy_silent = not any(term in f" {privacy_text.casefold()} " for term in privacy_terms)
        if privacy_silent:
            excerpt = (privacy_text or "Privacy page contains no readable text")[:300]
            evidence.append(_Evidence("governance_gap", "PRIVACY_SILENT_ON_AI", "present", 15, pages["privacy"].final_url, excerpt, _hash(privacy_text)))
        else:
            evidence.append(_Evidence("governance_gap", "PRIVACY_SILENT_ON_AI", "absent", 0, pages["privacy"].final_url, None, _hash(privacy_text)))

    policy_url_absent = "policy" not in pages and failures.get("policy") == "NOT_FOUND"
    if "policy" not in pages and not policy_url_absent:
        evidence.append(_Evidence("governance_gap", "AI_POLICY_STATUS", "unknown", 0, failure_urls.get("policy", targets["policy"]), None, _hash(failures.get("policy", "unknown"))))
    elif "policy" in pages:
        policy_excerpt = texts["policy"][:300] or "AI policy page returned readable HTML"
        evidence.append(_Evidence("governance_gap", "AI_POLICY_STATUS", "present", 0, pages["policy"].final_url, policy_excerpt, _hash(texts["policy"])))

    score = min(100, sum(item.weight for item in evidence if item.state == "present"))
    temperature = "HOT" if score >= 75 else "WARM" if score >= 55 else "COOL"
    page_hash = _hash("\0".join(page.html for page in pages.values()))
    try:
        conn.execute("BEGIN IMMEDIATE")
        _assert_firm_snapshot_current(conn, firm_id=firm_id, snapshot=firm)
        _assert_verification_current(
            conn,
            firm_id=firm_id,
            verification_attempt_id=verification_attempt_id,
            now=now,
        )
        _assert_reprocessing_current(
            conn,
            firm_id=firm_id,
            website_url=website,
            website_evidence_event_id=website_evidence_event_id,
            company_verification_attempt_id=company_verification_attempt_id,
            processing_input_hash=effective_input_hash,
            reprocessing_job_id=reprocessing_job_id,
            source_record_hash=firm["source_record_hash"],
            now=now,
        )
        run_id = conn.execute(
            """
            INSERT INTO enrichment_runs (
                firm_id, state, started_at, website_url, input_hash,
                website_evidence_event_id, company_verification_attempt_id
            ) VALUES (?, 'running', ?, ?, ?, ?, ?)
            """,
            (
                firm_id, timestamp, website, effective_input_hash,
                website_evidence_event_id, company_verification_attempt_id,
            ),
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
            (timestamp, pages["home"].final_url, page_hash, score, temperature, run_id),
        )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return EnrichmentResult(run_id=int(run_id), score=score, temperature=temperature)
