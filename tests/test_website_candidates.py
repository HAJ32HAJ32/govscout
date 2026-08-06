import json
from datetime import UTC, datetime
import sqlite3

import pytest

from govscout.db import connect_database, migrate
from govscout.fca_pipeline import verify_firm
from govscout.website_candidates import SearxngWebsiteCandidateProvider
from govscout.website_candidates import discover_website_candidates
from tests.test_processing_queue import NOW, _companies_house, _queue_firm


class NoCallProvider:
    def search(self, *, query, limit):
        raise AssertionError("provider must not be called inside a caller transaction")


class OneWebsiteProvider:
    def search(self, *, query, limit):
        from govscout.website_candidates import WebsiteCandidate

        return [
            WebsiteCandidate(
                website_url="https://official.example.test/",
                source_url="https://official.example.test/",
                title="Official website",
                snippet="The regulated firm's official website.",
            )
        ]


def test_searxng_provider_uses_bounded_json_search_and_maps_results():
    calls = []

    def requester(url, *, max_bytes):
        calls.append((url, max_bytes))
        return json.dumps(
            {
                "results": [
                    {
                        "url": "https://example.test/",
                        "title": "Example Ltd — Official website",
                        "content": "The official website for Example Ltd.",
                    }
                ]
            }
        ).encode()

    provider = SearxngWebsiteCandidateProvider(
        "https://search.mise.internal/search", requester=requester
    )
    results = provider.search(query='"Example Ltd" official website', limit=8)

    assert calls == [
        (
            "https://search.mise.internal/search?q=%22Example+Ltd%22+official+website&format=json&safesearch=1",
            262144,
        )
    ]
    assert len(results) == 1
    assert results[0].website_url == "https://example.test/"
    assert results[0].source_url == "https://example.test/"


def test_searxng_provider_rejects_non_https_or_ambiguous_endpoints():
    for endpoint in (
        "http://search.example.test/search",
        "https://user@search.example.test/search",
        "https://search.example.test/search?format=json",
        "https://search.example.test/other",
    ):
        with pytest.raises(ValueError):
            SearxngWebsiteCandidateProvider(endpoint, requester=lambda *_args, **_kwargs: b"{}")


def test_candidate_discovery_never_commits_a_callers_transaction(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO app_state (key, value) VALUES ('caller', 'uncommitted')")

    with pytest.raises(sqlite3.OperationalError, match="requires no active transaction"):
        discover_website_candidates(
            conn,
            firm_id=1,
            provider=NoCallProvider(),
            now=datetime.now(UTC),
        )

    assert conn.in_transaction is True
    conn.rollback()
    assert conn.execute(
        "SELECT value FROM app_state WHERE key = 'caller'"
    ).fetchone() is None


def test_database_rejects_candidate_provenance_and_unsafe_urls(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    _queue_firm(conn, website=None)
    firm = conn.execute("SELECT * FROM fca_firms WHERE frn = '123456'").fetchone()
    verification = verify_firm(
        conn,
        firm_id=firm["id"],
        companies_house=_companies_house(),
        now=NOW,
    )
    assert verification.verified is True

    with pytest.raises(sqlite3.IntegrityError, match="provenance"):
        conn.execute(
            """
            INSERT INTO website_candidate_searches (
                firm_id, fca_source_record_hash, company_verification_attempt_id,
                query, searched_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (firm["id"], "b" * 64, verification.attempt_id, "official website", NOW.isoformat()),
        )

    search_id = discover_website_candidates(
        conn,
        firm_id=firm["id"],
        provider=OneWebsiteProvider(),
        now=NOW,
    )
    with pytest.raises(sqlite3.IntegrityError, match="canonical HTTPS"):
        conn.execute(
            """
            INSERT INTO website_candidates (
                search_id, rank, website_url, source_url, title, snippet
            ) VALUES (?, 2, 'javascript:alert(1)', 'data:text/plain,bad',
                      'Unsafe result', 'Unsafe result')
            """,
            (search_id,),
        )
