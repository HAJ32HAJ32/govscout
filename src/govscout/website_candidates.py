from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import re
import sqlite3
from typing import Callable, Protocol
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from govscout.website_research import (
    WebsiteResearchConflict,
    _canonical_url,
    confirm_website_and_enqueue,
)

VERIFICATION_MAX_AGE = timedelta(days=30)
EXCLUDED_HOSTS = {
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "x.com",
    "twitter.com",
    "youtube.com",
    "find-and-update.company-information.service.gov.uk",
    "register.fca.org.uk",
}
_LEGAL_SUFFIXES = frozenset({"limited", "ltd", "llp", "plc", "group", "holdings"})
_MIN_MATCH_LENGTH = 4


@dataclass(frozen=True, slots=True)
class WebsiteCandidate:
    website_url: str
    title: str
    snippet: str
    source_url: str


class WebsiteCandidateProvider(Protocol):
    def search(self, *, query: str, limit: int) -> list[WebsiteCandidate]: ...


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _request_json(url: str, *, max_bytes: int) -> bytes:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "GovScout/1.0 website-candidate-search",
        },
    )
    try:
        response = build_opener(_NoRedirects).open(request, timeout=15)
    except HTTPError as exc:
        raise WebsiteResearchConflict(f"website search returned HTTP {exc.code}") from exc
    with response:
        content_type = response.headers.get_content_type()
        if response.status != 200 or content_type != "application/json":
            raise WebsiteResearchConflict("website search returned an invalid response")
        payload = response.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise WebsiteResearchConflict("website search response was too large")
    return payload


class SearxngWebsiteCandidateProvider:
    def __init__(
        self,
        endpoint: str,
        *,
        requester: Callable[..., bytes] = _request_json,
    ) -> None:
        try:
            parsed = urlsplit(endpoint)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("invalid SearXNG endpoint") from exc
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.hostname != parsed.hostname.lower()
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
            or parsed.path != "/search"
            or parsed.query
            or parsed.fragment
            or endpoint != f"https://{parsed.hostname}/search"
        ):
            raise ValueError("SearXNG endpoint must be a canonical HTTPS /search URL")
        self._endpoint = endpoint
        self._requester = requester

    def search(self, *, query: str, limit: int) -> list[WebsiteCandidate]:
        if not isinstance(query, str) or not 1 <= len(query) <= 500:
            raise ValueError("website search query must be between 1 and 500 characters")
        if type(limit) is not int or not 1 <= limit <= 8:
            raise ValueError("website search limit must be between 1 and 8")
        url = f"{self._endpoint}?{urlencode({'q': query, 'format': 'json', 'safesearch': 1})}"
        payload = self._requester(url, max_bytes=262_144)
        try:
            document = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WebsiteResearchConflict("website search returned invalid JSON") from exc
        if not isinstance(document, dict) or not isinstance(document.get("results"), list):
            raise WebsiteResearchConflict("website search response shape changed")
        results: list[WebsiteCandidate] = []
        for raw in document["results"][:limit]:
            if not isinstance(raw, dict):
                continue
            result_url = raw.get("url")
            title = raw.get("title")
            snippet = raw.get("content", "")
            if not all(isinstance(value, str) for value in (result_url, title, snippet)):
                continue
            results.append(
                WebsiteCandidate(
                    website_url=result_url,
                    title=title,
                    snippet=snippet,
                    source_url=result_url,
                )
            )
        return results


def _excluded(hostname: str) -> bool:
    return any(hostname == host or hostname.endswith(f".{host}") for host in EXCLUDED_HOSTS)


def _bounded_text(value: str, *, maximum: int, field: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    clean = value.strip()
    if (not clean and not allow_empty) or len(clean) > maximum or "\x00" in clean:
        raise ValueError(f"invalid {field}")
    return clean


def candidate_urls_are_safe(*, website_url: str, source_url: str) -> bool:
    """Defence-in-depth for links loaded from persistent candidate history."""
    try:
        website = _canonical_url(website_url, field="website URL", allow_query=False)
        source = _canonical_url(source_url, field="evidence URL", allow_query=True)
    except (TypeError, ValueError):
        return False
    return website == website_url and source == source_url


def _normalise_for_match(text: str) -> str:
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return "".join(token for token in tokens if token not in _LEGAL_SUFFIXES)


def _is_confident_domain_match(*, firm_name: str, website_url: str) -> bool:
    """Conservative name/domain check for auto-confirming a candidate with no human review.

    Deliberately strict: only an exact match, or the firm's full name starting with
    the domain label, counts as confident. Anything else falls through to the
    existing manual candidate-review flow rather than guessing.
    """
    hostname = urlsplit(website_url).hostname or ""
    if hostname.startswith("www."):
        hostname = hostname[len("www.") :]
    label = hostname.split(".")[0] if hostname else ""
    normalised_label = re.sub(r"[^a-z0-9]", "", label.lower())
    normalised_name = _normalise_for_match(firm_name)
    if len(normalised_label) < _MIN_MATCH_LENGTH or not normalised_name:
        return False
    return normalised_label == normalised_name or normalised_name.startswith(normalised_label)


def auto_confirm_high_confidence_website(
    conn: sqlite3.Connection,
    *,
    firm_id: int,
    firm_name: str,
    provider: WebsiteCandidateProvider,
    now: datetime,
) -> str | None:
    """Search for candidates and, only if the top one is an unambiguous name/domain
    match, confirm it exactly as the manual `/today` confirm button would, without
    waiting for a human click.

    Returns the confirmed website URL, or None if no confident match was found
    (the firm is left with its candidates sitting in the existing manual review UI).
    Confirming enqueues a reprocessing job the same way manual confirmation does —
    it does not run enrichment synchronously here.
    """
    try:
        search_id = discover_website_candidates(conn, firm_id=firm_id, provider=provider, now=now)
    except WebsiteResearchConflict:
        return None
    top = conn.execute(
        "SELECT website_url, source_url FROM website_candidates WHERE search_id = ? AND rank = 1",
        (search_id,),
    ).fetchone()
    if top is None or not _is_confident_domain_match(
        firm_name=firm_name, website_url=top["website_url"]
    ):
        return None
    confirm_website_and_enqueue(
        conn,
        firm_id=firm_id,
        website_url=top["website_url"],
        evidence_url=top["source_url"],
        justification="Auto-confirmed: candidate domain closely matches the firm's registered name.",
        actor="govscout-auto-confirm",
        expected_previous_event_id=None,
        request_reason="Auto-confirmed suggested website via high-confidence name/domain match.",
        now=now,
    )
    return top["website_url"]


def discover_website_candidates(
    conn: sqlite3.Connection,
    *,
    firm_id: int,
    provider: WebsiteCandidateProvider,
    now: datetime,
) -> int:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if conn.in_transaction:
        raise sqlite3.OperationalError("website candidate discovery requires no active transaction")
    firm = conn.execute("SELECT * FROM fca_firms WHERE id = ?", (firm_id,)).fetchone()
    if firm is None:
        raise KeyError(firm_id)
    attempt = conn.execute(
        "SELECT * FROM company_verification_attempts WHERE firm_id = ? ORDER BY id DESC LIMIT 1",
        (firm_id,),
    ).fetchone()
    now_utc = now.astimezone(UTC)
    if (
        not firm["is_active"]
        or not firm["company_number"]
        or attempt is None
        or attempt["state"] != "verified"
        or attempt["company_number"] != firm["company_number"]
        or attempt["fca_source_record_hash"] != firm["source_record_hash"]
    ):
        raise WebsiteResearchConflict("current Companies House verification is required")
    checked_at = datetime.fromisoformat(attempt["checked_at"]).astimezone(UTC)
    if checked_at > now_utc or now_utc - checked_at > VERIFICATION_MAX_AGE:
        raise WebsiteResearchConflict("Companies House verification is stale or future-dated")
    query = f'"{firm["firm_name"]}" "{firm["company_number"]}" official website'
    recent = conn.execute(
        """
        SELECT id, searched_at FROM website_candidate_searches
        WHERE firm_id = ? AND fca_source_record_hash = ?
          AND company_verification_attempt_id = ? AND query = ?
        ORDER BY id DESC LIMIT 1
        """,
        (firm_id, firm["source_record_hash"], attempt["id"], query),
    ).fetchone()
    if recent is not None:
        searched_at = datetime.fromisoformat(recent["searched_at"]).astimezone(UTC)
        if searched_at <= now_utc and now_utc - searched_at <= timedelta(minutes=5):
            return int(recent["id"])
    searches_today = conn.execute(
        """
        SELECT count(*) FROM website_candidate_searches
        WHERE firm_id = ? AND julianday(searched_at) >= julianday(?) - 1
        """,
        (firm_id, now_utc.isoformat()),
    ).fetchone()[0]
    if searches_today >= 20:
        raise WebsiteResearchConflict("website candidate search limit reached for this firm")
    raw_results = provider.search(query=query, limit=8)
    candidates: list[WebsiteCandidate] = []
    seen_hosts: set[str] = set()
    for result in raw_results:
        try:
            website = _canonical_url(result.website_url, field="website URL", allow_query=False)
            source = _canonical_url(result.source_url, field="evidence URL", allow_query=True)
        except ValueError:
            continue
        hostname = urlsplit(website).hostname
        if hostname is None or _excluded(hostname) or hostname in seen_hosts:
            continue
        seen_hosts.add(hostname)
        candidates.append(
            WebsiteCandidate(
                website_url=website,
                title=_bounded_text(result.title, maximum=300, field="candidate title"),
                snippet=_bounded_text(
                    result.snippet, maximum=1000, field="candidate snippet", allow_empty=True
                ),
                source_url=source,
            )
        )
        if len(candidates) == 3:
            break
    if not candidates:
        raise WebsiteResearchConflict("no plausible official websites were found")
    try:
        conn.execute("BEGIN IMMEDIATE")
        current_firm = conn.execute(
            "SELECT source_record_hash FROM fca_firms WHERE id = ?", (firm_id,)
        ).fetchone()
        current_attempt = conn.execute(
            "SELECT id FROM company_verification_attempts WHERE firm_id = ? ORDER BY id DESC LIMIT 1",
            (firm_id,),
        ).fetchone()
        if (
            current_firm is None
            or current_firm["source_record_hash"] != firm["source_record_hash"]
            or current_attempt is None
            or current_attempt["id"] != attempt["id"]
        ):
            raise WebsiteResearchConflict("firm identity changed during website search")
        search_id = conn.execute(
            """
            INSERT INTO website_candidate_searches (
                firm_id, fca_source_record_hash, company_verification_attempt_id,
                query, searched_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                firm_id,
                firm["source_record_hash"],
                attempt["id"],
                query,
                now_utc.isoformat(),
            ),
        ).lastrowid
        if search_id is None:
            raise RuntimeError("SQLite did not return a candidate search id")
        for rank, candidate in enumerate(candidates, start=1):
            conn.execute(
                """
                INSERT INTO website_candidates (
                    search_id, rank, website_url, source_url, title, snippet
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    search_id,
                    rank,
                    candidate.website_url,
                    candidate.source_url,
                    candidate.title,
                    candidate.snippet,
                ),
            )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return int(search_id)


def load_confirmable_candidate(
    conn: sqlite3.Connection, *, firm_id: int, candidate_id: int
) -> sqlite3.Row:
    candidate = conn.execute(
        """
        SELECT c.*, s.fca_source_record_hash, s.company_verification_attempt_id
        FROM website_candidates AS c
        JOIN website_candidate_searches AS s ON s.id = c.search_id
        WHERE c.id = ? AND s.firm_id = ?
          AND s.id = (
              SELECT id FROM website_candidate_searches
              WHERE firm_id = ? ORDER BY id DESC LIMIT 1
          )
        """,
        (candidate_id, firm_id, firm_id),
    ).fetchone()
    if candidate is None:
        raise WebsiteResearchConflict("website candidate is stale or missing")
    firm = conn.execute(
        "SELECT source_record_hash FROM fca_firms WHERE id = ?", (firm_id,)
    ).fetchone()
    attempt = conn.execute(
        "SELECT id FROM company_verification_attempts WHERE firm_id = ? ORDER BY id DESC LIMIT 1",
        (firm_id,),
    ).fetchone()
    if (
        firm is None
        or firm["source_record_hash"] != candidate["fca_source_record_hash"]
        or attempt is None
        or attempt["id"] != candidate["company_verification_attempt_id"]
    ):
        raise WebsiteResearchConflict("website candidate dependencies changed")
    if not candidate_urls_are_safe(
        website_url=candidate["website_url"], source_url=candidate["source_url"]
    ):
        raise WebsiteResearchConflict("website candidate URL is unsafe")
    return candidate
