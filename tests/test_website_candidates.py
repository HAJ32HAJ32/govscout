import json
from datetime import UTC, datetime, timedelta
import sqlite3

import pytest

from govscout.db import connect_database, migrate
from govscout.fca_pipeline import verify_firm
from govscout.processing_queue import run_pending_jobs
from govscout.website_candidates import SearxngWebsiteCandidateProvider
from govscout.website_candidates import discover_website_candidates
from govscout.website_candidates import (
    WebsiteCandidate,
    _is_confident_domain_match,
    auto_confirm_high_confidence_website,
)
from tests.test_processing_queue import NOW, FakeSiteTransport, _companies_house, _queue_firm


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


@pytest.mark.parametrize(
    ("firm_name", "website_url", "expected"),
    [
        ("Example Finance Ltd", "https://examplefinance.test/", True),
        ("Example Finance Ltd", "https://www.examplefinance.test/", True),
        ("Example Finance Group Holdings LLP", "https://examplefinance.test/", True),
        ("Example Finance Ltd", "https://totallydifferent.test/", False),
        ("Example Finance Ltd", "https://ex.test/", False),
    ],
)
def test_is_confident_domain_match(firm_name, website_url, expected):
    assert (
        _is_confident_domain_match(firm_name=firm_name, website_url=website_url) is expected
    )


class _MatchingProvider:
    def search(self, *, query, limit):
        return [
            WebsiteCandidate(
                website_url="https://examplefinance.test/",
                source_url="https://search.example.test/results?q=example",
                title="Example Finance Ltd — Official site",
                snippet="Example Finance Ltd, FCA regulated financial advice.",
            )
        ]


class _AmbiguousProvider:
    def search(self, *, query, limit):
        return [
            WebsiteCandidate(
                website_url="https://totallydifferent.test/",
                source_url="https://search.example.test/results?q=example",
                title="Some other site",
                snippet="Not obviously the firm's official site.",
            )
        ]


def test_auto_confirm_high_confidence_website_asserts_matching_candidate(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    _queue_firm(conn, website=None)
    firm = conn.execute("SELECT * FROM fca_firms WHERE frn = '123456'").fetchone()
    verify_firm(conn, firm_id=firm["id"], companies_house=_companies_house(), now=NOW)

    confirmed = auto_confirm_high_confidence_website(
        conn,
        firm_id=firm["id"],
        firm_name=firm["firm_name"],
        provider=_MatchingProvider(),
        now=NOW,
    )

    assert confirmed == "https://examplefinance.test/"
    event = conn.execute(
        "SELECT actor, website_url, action FROM firm_website_evidence_events"
    ).fetchone()
    assert event["actor"] == "govscout-auto-confirm"
    assert event["action"] == "assert"
    assert event["website_url"] == "https://examplefinance.test/"
    assert conn.execute("SELECT count(*) FROM fca_reprocessing_jobs").fetchone()[0] == 1


def test_auto_confirm_high_confidence_website_skips_ambiguous_candidate(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    _queue_firm(conn, website=None)
    firm = conn.execute("SELECT * FROM fca_firms WHERE frn = '123456'").fetchone()
    verify_firm(conn, firm_id=firm["id"], companies_house=_companies_house(), now=NOW)

    confirmed = auto_confirm_high_confidence_website(
        conn,
        firm_id=firm["id"],
        firm_name=firm["firm_name"],
        provider=_AmbiguousProvider(),
        now=NOW,
    )

    assert confirmed is None
    assert conn.execute(
        "SELECT count(*) FROM firm_website_evidence_events"
    ).fetchone()[0] == 0
    # candidates are still recorded for the existing manual-review UI
    assert conn.execute("SELECT count(*) FROM website_candidates").fetchone()[0] == 1


def test_worker_auto_confirms_high_confidence_website_and_scores(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    _queue_firm(conn, website=None)
    site = FakeSiteTransport(
        {
            "https://examplefinance.test/": "AI-powered FCA regulated advice.",
            "https://examplefinance.test/privacy": "Privacy and automated decisions.",
            "https://examplefinance.test/careers": "Our team uses Copilot.",
            "https://examplefinance.test/ai-policy": "Our AI governance policy.",
        }
    )

    # The initial FCA processing job auto-confirms a website (which enqueues a
    # reprocessing job) but still finishes WEBSITE_MISSING itself, since
    # fca_firms.website_url is deliberately never overwritten in place. With
    # limit=5 the worker keeps claiming within the same call, so it also drains
    # the newly-enqueued reprocessing job and produces a real score in one pass
    # (in production, with --limit 1, this takes two ~60s ticks instead).
    result = run_pending_jobs(
        conn,
        companies_house=_companies_house(),
        site_transport=site,
        now=NOW + timedelta(seconds=2),
        limit=5,
        website_candidate_provider=_MatchingProvider(),
    )
    assert (result.claimed, result.succeeded, result.failed, result.retried) == (2, 1, 1, 0)
    firm = conn.execute("SELECT id FROM fca_firms WHERE frn = '123456'").fetchone()
    event = conn.execute(
        "SELECT actor FROM firm_website_evidence_events WHERE firm_id = ?", (firm["id"],)
    ).fetchone()
    assert event["actor"] == "govscout-auto-confirm"
    run = conn.execute(
        "SELECT score, state FROM enrichment_runs WHERE firm_id = ?", (firm["id"],)
    ).fetchone()
    assert run["state"] == "complete"
    assert run["score"] is not None


def test_worker_leaves_ambiguous_candidate_for_manual_review(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    _queue_firm(conn, website=None)

    result = run_pending_jobs(
        conn,
        companies_house=_companies_house(),
        site_transport=FakeSiteTransport({}),
        now=NOW + timedelta(seconds=2),
        limit=5,
        website_candidate_provider=_AmbiguousProvider(),
    )

    assert (result.claimed, result.succeeded, result.failed, result.retried) == (1, 0, 1, 0)
    job = conn.execute("SELECT outcome_code FROM fca_processing_jobs").fetchone()
    assert job["outcome_code"] == "WEBSITE_MISSING"
    assert conn.execute(
        "SELECT count(*) FROM firm_website_evidence_events"
    ).fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM website_candidates").fetchone()[0] == 1
