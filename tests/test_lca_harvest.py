from datetime import UTC, datetime
from email.message import Message
import sqlite3
from urllib.request import Request

import pytest

from govscout.db import connect_database, migrate
from govscout.lca_harvest import (
    HarvestResult,
    LCA_DIRECTORY_URL,
    LcaCandidateConflict,
    LcaDirectoryEntry,
    LcaDirectoryFormatError,
    NoRedirectHandler,
    UrlLcaDirectoryTransport,
    harvest_lca,
    parse_lca_directory,
    stage_lca_candidates,
)


def test_parse_lca_directory_extracts_public_member_cards_in_source_order():
    html = """
    <main>
      <a href="/privacy-policy/">Privacy policy</a>
      <div class="lca-az-index">
      <li class="lca-az-item">
        <a class="lca-az-link featured" data-name="Alpha &amp; Beta Limited"
           href="https://www.legionellacontrolassociation.co.uk/company/alpha-beta/">
          <strong>Alpha &amp; Beta Limited</strong><span>Kingston upon Thames</span>
        </a>
      </li>
      <li class="lca-az-item">
        <a class="lca-az-link"
           href="https://www.legionellacontrolassociation.co.uk/company/zeta-compliance/">
          <strong>Zeta Compliance Ltd</strong><span>Bicester</span>
        </a>
      </li>
      </div>
    </main>
    """

    assert parse_lca_directory(html) == (
        LcaDirectoryEntry(
            company_name="Alpha & Beta Limited",
            source_location="Kingston upon Thames",
            source_url=(
                "https://www.legionellacontrolassociation.co.uk/company/alpha-beta/"
            ),
        ),
        LcaDirectoryEntry(
            company_name="Zeta Compliance Ltd",
            source_location="Bicester",
            source_url=(
                "https://www.legionellacontrolassociation.co.uk/company/zeta-compliance/"
            ),
        ),
    )


def test_parse_lca_directory_preserves_member_with_no_published_location():
    html = """
    <div class="lca-az-index">
    <a class="lca-az-link"
       href="https://www.legionellacontrolassociation.co.uk/company/07315098/">
      <strong>ECB (Water Solutions) Ltd</strong><span></span>
    </a>
    </div>
    """

    assert parse_lca_directory(html) == (
        LcaDirectoryEntry(
            company_name="ECB (Water Solutions) Ltd",
            source_location=None,
            source_url=(
                "https://www.legionellacontrolassociation.co.uk/company/07315098/"
            ),
        ),
    )


def test_parse_lca_directory_fails_closed_when_member_cards_are_missing():
    with pytest.raises(LcaDirectoryFormatError, match="no valid member cards"):
        parse_lca_directory("<html><main>Directory temporarily unavailable</main></html>")


def test_parse_lca_directory_rejects_duplicate_member_urls():
    card = """
    <a class="lca-az-link"
       href="https://www.legionellacontrolassociation.co.uk/company/example/">
      <strong>Example Limited</strong><span>London</span>
    </a>
    """

    with pytest.raises(LcaDirectoryFormatError, match="duplicate member URL"):
        parse_lca_directory(f'<div class="lca-az-index">{card}{card}</div>')


def test_parse_lca_directory_rejects_one_malformed_card_among_valid_cards():
    html = """
    <div class="lca-az-index">
    <a class="lca-az-link"
       href="https://www.legionellacontrolassociation.co.uk/company/example/">
      <strong>Example Limited</strong><span>London</span>
    </a>
    <a class="lca-az-link" href="https://attacker.example/company/injected/">
      <strong>Injected Limited</strong><span>London</span>
    </a>
    </div>
    """

    with pytest.raises(LcaDirectoryFormatError, match="invalid member card"):
        parse_lca_directory(html)


def test_parse_lca_directory_rejects_truncated_member_list():
    html = """
    <div class="lca-az-index">
      <div><a class="lca-az-link"
         href="https://www.legionellacontrolassociation.co.uk/company/example/">
        <strong>Example Limited</strong><span>London</span>
      </a>
    """

    with pytest.raises(LcaDirectoryFormatError, match="incomplete member list"):
        parse_lca_directory(html)


def test_stage_lca_candidates_uses_bounded_spread_without_creating_leads(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    entries = tuple(
        LcaDirectoryEntry(
            company_name=f"{name} Limited",
            source_location=location,
            source_url=(
                "https://www.legionellacontrolassociation.co.uk/company/"
                f"{name.lower()}/"
            ),
        )
        for name, location in (
            ("Alpha", "London"),
            ("Bravo", "Bristol"),
            ("Charlie", "Leeds"),
            ("Delta", "Glasgow"),
        )
    )

    result = stage_lca_candidates(
        conn,
        entries,
        limit=2,
        now=datetime(2026, 7, 20, 14, 0, tzinfo=UTC),
    )

    assert result == HarvestResult(source_count=4, staged_count=2, created_count=2)
    names = [
        row[0]
        for row in conn.execute("SELECT company_name FROM candidates ORDER BY id")
    ]
    assert names == ["Alpha Limited", "Charlie Limited"]
    assert conn.execute("SELECT count(*) FROM leads").fetchone()[0] == 0


def test_stage_lca_candidates_rejects_changed_source_record_without_rewriting(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    source_url = "https://www.legionellacontrolassociation.co.uk/company/example/"
    first = LcaDirectoryEntry("Example Limited", "London", source_url)
    changed = LcaDirectoryEntry("Example Limited", "Bristol", source_url)

    stage_lca_candidates(
        conn,
        (first,),
        limit=1,
        now=datetime(2026, 7, 20, 14, 0, tzinfo=UTC),
    )

    with pytest.raises(LcaCandidateConflict, match="source record changed"):
        stage_lca_candidates(
            conn,
            (changed,),
            limit=1,
            now=datetime(2026, 7, 21, 14, 0, tzinfo=UTC),
        )

    row = conn.execute(
        "SELECT source_location, last_seen_at FROM candidates WHERE source_url = ?",
        (source_url,),
    ).fetchone()
    assert tuple(row) == ("London", "2026-07-20T14:00:00+00:00")


def test_stage_lca_candidates_does_not_rollback_caller_transaction(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    entry = LcaDirectoryEntry(
        "Example Limited",
        "London",
        "https://www.legionellacontrolassociation.co.uk/company/example/",
    )
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO app_state (key, value) VALUES ('caller-work', 'present')")

    with pytest.raises(sqlite3.OperationalError, match="active transaction"):
        stage_lca_candidates(
            conn,
            (entry,),
            limit=1,
            now=datetime(2026, 7, 20, 14, 0, tzinfo=UTC),
        )

    assert conn.in_transaction
    assert conn.execute(
        "SELECT value FROM app_state WHERE key = 'caller-work'"
    ).fetchone()[0] == "present"
    conn.execute("ROLLBACK")


def test_stage_lca_candidates_rejects_stale_observation_time(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    entry = LcaDirectoryEntry(
        "Example Limited",
        "London",
        "https://www.legionellacontrolassociation.co.uk/company/example/",
    )
    stage_lca_candidates(
        conn,
        (entry,),
        limit=1,
        now=datetime(2026, 7, 21, 14, 0, tzinfo=UTC),
    )

    with pytest.raises(LcaCandidateConflict, match="older than last observation"):
        stage_lca_candidates(
            conn,
            (entry,),
            limit=1,
            now=datetime(2026, 7, 20, 14, 0, tzinfo=UTC),
        )

    last_seen_at = conn.execute(
        "SELECT last_seen_at FROM candidates WHERE source_url = ?",
        (entry.source_url,),
    ).fetchone()[0]
    assert last_seen_at == "2026-07-21T14:00:00+00:00"


def test_harvest_lca_fetches_only_the_fixed_official_directory(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)

    class RecordingTransport:
        def __init__(self):
            self.urls = []

        def fetch_html(self, url):
            self.urls.append(url)
            return """
            <div class="lca-az-index">
            <a class="lca-az-link"
               href="https://www.legionellacontrolassociation.co.uk/company/example/">
              <strong>Example Limited</strong><span>London</span>
            </a>
            </div>
            """

    transport = RecordingTransport()
    result = harvest_lca(
        conn,
        transport,
        limit=1,
        now=datetime(2026, 7, 20, 14, 0, tzinfo=UTC),
    )

    assert transport.urls == [LCA_DIRECTORY_URL]
    assert result == HarvestResult(source_count=1, staged_count=1, created_count=1)


def test_harvest_lca_rejects_invalid_limit_before_fetching(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)

    class RecordingTransport:
        def __init__(self):
            self.urls = []

        def fetch_html(self, url):
            self.urls.append(url)
            return "not reached"

    transport = RecordingTransport()

    with pytest.raises(ValueError, match="between 1 and 50"):
        harvest_lca(
            conn,
            transport,
            limit=0,
            now=datetime(2026, 7, 20, 14, 0, tzinfo=UTC),
        )

    assert transport.urls == []


def test_url_transport_fetches_bounded_html_with_identifiable_user_agent():
    html = b"<html><body>LCA directory</body></html>"

    class Response:
        def __init__(self):
            self.headers = Message()
            self.headers["Content-Type"] = "text/html; charset=utf-8"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return LCA_DIRECTORY_URL

        def read(self, size):
            assert size == 1_000_001
            return html

    calls = []

    def opener(request, *, timeout):
        calls.append((request, timeout))
        return Response()

    transport = UrlLcaDirectoryTransport(opener=opener)

    assert transport.fetch_html(LCA_DIRECTORY_URL) == html.decode()
    request, timeout = calls[0]
    assert request.full_url == LCA_DIRECTORY_URL
    assert request.get_header("User-agent").startswith("GovScout/")
    assert timeout == 20


def test_redirect_handler_creates_no_follow_up_request():
    handler = NoRedirectHandler()
    request = Request(LCA_DIRECTORY_URL)

    assert (
        handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {"Location": "http://127.0.0.1/private"},
            "http://127.0.0.1/private",
        )
        is None
    )


def test_url_transport_rejects_response_over_one_megabyte():
    class OversizedResponse:
        def __init__(self):
            self.headers = Message()
            self.headers["Content-Type"] = "text/html; charset=utf-8"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return LCA_DIRECTORY_URL

        def read(self, size):
            return b"x" * size

    transport = UrlLcaDirectoryTransport(
        opener=lambda _request, *, timeout: OversizedResponse()
    )

    with pytest.raises(LcaDirectoryFormatError, match="exceeded 1 MB"):
        transport.fetch_html(LCA_DIRECTORY_URL)
