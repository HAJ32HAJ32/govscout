from datetime import UTC, datetime, timedelta
import sqlite3

import pytest

from govscout.companies_house import CompaniesHouseClient
from govscout.db import connect_database, migrate
from govscout.enrichment import SiteFetchError, SitePage, run_enrichment
from govscout.fca_discovery import ingest_fca_records, parse_fca_json
from govscout.fca_pipeline import verify_and_promote_firm
from govscout.quality import is_outreach_ready, review_firm, run_qc
from tests.support import StubCompaniesHouseTransport


NOW = datetime(2026, 7, 25, 10, tzinfo=UTC)


class FakeSiteTransport:
    def __init__(self, pages):
        self.pages = pages
        self.urls = []

    def fetch_html(self, url):
        self.urls.append(url)
        value = self.pages.get(url)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise SiteFetchError("NOT_FOUND")
        return SitePage(url=url, final_url=url, html=value, fetched_at=NOW)


def _stage(conn, *, frn="123456", website="https://example.test/", observed=NOW):
    payload = (
        "{\"firms\":[{"
        f'"frn":"{frn}","firm_name":"Example Finance Ltd",'
        '"status":"Authorised","firm_type":"Regulated firm",'
        f'"source_url":"https://register.fca.org.uk/s/firm?id={frn}",'
        f'"website_url":"{website}","location":"London",'
        '"company_number":"12345678"}]}'
    ).encode()
    ingest_fca_records(conn, parse_fca_json(payload), limit=1, now=observed)
    return conn.execute("SELECT id FROM fca_firms WHERE frn = ?", (frn,)).fetchone()[0]


def _promote(conn, firm_id, *, verified_at=NOW):
    return verify_and_promote_firm(
        conn,
        firm_id=firm_id,
        companies_house=CompaniesHouseClient(
            StubCompaniesHouseTransport(
                {
                    "company_number": "12345678",
                    "company_name": "Example Finance Ltd",
                    "company_status": "active",
                    "type": "ltd",
                }
            )
        ),
        contact_email="compliance@example.test",
        now=verified_at,
    )


def _approve_current_firm(conn, firm_id, *, now=NOW):
    _promote(conn, firm_id, verified_at=now)
    transport = FakeSiteTransport(
        {
            "https://example.test/": "FCA regulated. AI-powered advice.",
            "https://example.test/privacy": "Privacy and automated decision safeguards.",
            "https://example.test/careers": "We use Copilot.",
            "https://example.test/ai-policy": "Our AI governance policy explains oversight.",
        }
    )
    run_enrichment(conn, firm_id=firm_id, transport=transport, now=now)
    qc = run_qc(conn, firm_id=firm_id, now=now)
    assert qc.passed
    review_firm(
        conn,
        firm_id=firm_id,
        decision="approved",
        qc_run_id=qc.qc_run_id,
        notes="Checked",
        rejection_reason=None,
        now=now,
    )
    return qc


def test_pluggable_enrichment_persists_exact_evidence_and_hot_score(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    firm_id = _stage(conn)
    transport = FakeSiteTransport(
        {
            "https://example.test/": (
                "<h1>FCA regulated advice</h1><p>Our AI-powered assistant helps clients.</p>"
            ),
            "https://example.test/privacy": "<h1>Privacy</h1><p>Cookies and contact data.</p>",
            "https://example.test/careers": "<p>Our team uses Microsoft Copilot.</p>",
            "https://example.test/ai-policy": SiteFetchError("NOT_FOUND"),
        }
    )

    result = run_enrichment(conn, firm_id=firm_id, transport=transport, now=NOW)

    assert result.score == 100
    assert result.temperature == "HOT"
    evidence = conn.execute(
        """
        SELECT signal_group, code, evidence_state, source_url, excerpt
        FROM evidence_items WHERE run_id = ? ORDER BY code
        """,
        (result.run_id,),
    ).fetchall()
    assert {row[1] for row in evidence} >= {
        "FCA_REGULATED",
        "AI_VISIBLE",
        "PRIVACY_SILENT_ON_AI",
        "AI_POLICY_NOT_FOUND",
    }
    assert all(row[3] and row[4] for row in evidence if row[2] == "present")


def test_enrichment_records_honest_unknown_when_privacy_page_cannot_be_checked(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    firm_id = _stage(conn)
    transport = FakeSiteTransport(
        {
            "https://example.test/": "<p>FCA regulated financial advice.</p>",
            "https://example.test/privacy": SiteFetchError("TIMEOUT"),
            "https://example.test/careers": SiteFetchError("NOT_FOUND"),
            "https://example.test/ai-policy": SiteFetchError("NOT_FOUND"),
        }
    )

    result = run_enrichment(conn, firm_id=firm_id, transport=transport, now=NOW)
    rows = conn.execute(
        """
        SELECT code, evidence_state FROM evidence_items
        WHERE run_id = ? AND code LIKE '%_SCAN_STATUS'
        """,
        (result.run_id,),
    ).fetchall()

    assert {tuple(row) for row in rows} == {
        ("PRIVACY_SCAN_STATUS", "unknown"),
        ("CAREERS_SCAN_STATUS", "unknown"),
        ("POLICY_SCAN_STATUS", "unknown"),
    }
    assert result.temperature == "COOL"


def test_enrichment_rejects_fca_identity_changed_during_network_fetch(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    firm_id = _stage(conn)

    class MutatingTransport(FakeSiteTransport):
        def fetch_html(self, url):
            if not self.urls:
                conn.execute(
                    "UPDATE fca_firms SET website_url = 'https://changed.test/' WHERE id = ?",
                    (firm_id,),
                )
            return super().fetch_html(url)

    transport = MutatingTransport(
        {
            "https://example.test/": "FCA regulated. AI-powered advice.",
            "https://example.test/privacy": "Privacy and automated decisions.",
            "https://example.test/careers": "Copilot.",
            "https://example.test/ai-policy": "AI policy.",
        }
    )

    with pytest.raises(SiteFetchError, match="IDENTITY_CHANGED"):
        run_enrichment(conn, firm_id=firm_id, transport=transport, now=NOW)

    assert conn.execute("SELECT count(*) FROM enrichment_runs").fetchone()[0] == 0


def test_qc_rejects_complete_enrichment_for_unpromoted_firm(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    firm_id = _stage(conn)
    transport = FakeSiteTransport(
        {
            "https://example.test/": "FCA regulated. AI-powered advice.",
            "https://example.test/privacy": "Privacy and automated decision safeguards.",
            "https://example.test/careers": "We use Copilot.",
            "https://example.test/ai-policy": "Our AI governance policy explains oversight.",
        }
    )
    run_enrichment(conn, firm_id=firm_id, transport=transport, now=NOW)

    qc = run_qc(conn, firm_id=firm_id, now=NOW)

    assert not qc.passed
    assert "LEAD_MISSING" in qc.reasons
    assert not is_outreach_ready(conn, firm_id=firm_id, now=NOW)


def test_qc_fails_closed_then_human_approval_makes_current_good_data_ready(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    firm_id = _stage(conn)

    missing = run_qc(conn, firm_id=firm_id, now=NOW)
    assert not missing.passed
    assert "SCAN_MISSING" in missing.reasons
    assert not is_outreach_ready(conn, firm_id=firm_id, now=NOW)

    _promote(conn, firm_id)

    transport = FakeSiteTransport(
        {
            "https://example.test/": "FCA regulated. AI-powered advice.",
            "https://example.test/privacy": "Privacy and cookies only.",
            "https://example.test/careers": "We use Copilot.",
            "https://example.test/ai-policy": "Our AI governance policy explains oversight.",
        }
    )
    run_enrichment(conn, firm_id=firm_id, transport=transport, now=NOW)
    passed = run_qc(conn, firm_id=firm_id, now=NOW + timedelta(minutes=1))
    assert passed.passed
    assert not is_outreach_ready(conn, firm_id=firm_id, now=NOW + timedelta(minutes=1))

    review_firm(
        conn,
        firm_id=firm_id,
        decision="approved",
        qc_run_id=passed.qc_run_id,
        notes="Evidence checked",
        rejection_reason=None,
        now=NOW + timedelta(minutes=2),
    )
    assert is_outreach_ready(conn, firm_id=firm_id, now=NOW + timedelta(minutes=2))


def test_review_history_is_append_only_and_latest_decision_controls_readiness(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    firm_id = _stage(conn)
    _approve_current_firm(conn, firm_id)

    review_firm(
        conn,
        firm_id=firm_id,
        decision="rejected",
        qc_run_id=None,
        notes="Later review",
        rejection_reason="Not a suitable prospect",
        now=NOW + timedelta(minutes=1),
    )

    history = conn.execute(
        """
        SELECT id, decision, notes, rejection_reason
        FROM firm_reviews WHERE firm_id = ? ORDER BY id
        """,
        (firm_id,),
    ).fetchall()
    assert [tuple(row[1:]) for row in history] == [
        ("approved", "Checked", None),
        ("rejected", "Later review", "Not a suitable prospect"),
    ]
    assert not is_outreach_ready(conn, firm_id=firm_id, now=NOW + timedelta(minutes=1))

    for statement in (
        "UPDATE firm_reviews SET notes = 'rewritten' WHERE id = ?",
        "DELETE FROM firm_reviews WHERE id = ?",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="immutable|cannot be deleted"):
            conn.execute(statement, (history[0]["id"],))


def test_qc_rejects_stale_changed_duplicate_and_contradictory_evidence(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    firm_id = _stage(conn, observed=NOW - timedelta(days=40))
    _stage(conn, frn="234567")
    transport = FakeSiteTransport(
        {
            "https://example.test/": "FCA regulated. AI-powered advice.",
            "https://example.test/privacy": "Privacy only.",
            "https://example.test/careers": "Copilot.",
            "https://example.test/ai-policy": SiteFetchError("NOT_FOUND"),
        }
    )
    result = run_enrichment(conn, firm_id=firm_id, transport=transport, now=NOW)
    original_run_id = result.run_id
    run_id = conn.execute(
        """
        INSERT INTO enrichment_runs (
            firm_id, state, started_at, website_url, input_hash
        )
        SELECT firm_id, 'running', started_at, website_url, input_hash
        FROM enrichment_runs WHERE id = ?
        """,
        (original_run_id,),
    ).lastrowid
    conn.execute(
        """
        INSERT INTO evidence_items (
            run_id, signal_group, code, evidence_state, weight,
            source_url, excerpt, observed_at, content_hash
        )
        SELECT ?, signal_group, code, evidence_state, weight,
               source_url, excerpt, observed_at, content_hash
        FROM evidence_items WHERE run_id = ?
        """,
        (run_id, original_run_id),
    )
    source = "https://example.test/"
    conn.execute(
        """
        INSERT INTO evidence_items (
            run_id, signal_group, code, evidence_state, weight,
            source_url, excerpt, observed_at, content_hash
        ) VALUES (?, 'ai_exposure', 'AI_VISIBLE', 'absent', 0, ?, NULL, ?, ?)
        """,
        (run_id, source + "other", NOW.isoformat(), "f" * 64),
    )
    conn.execute(
        """
        UPDATE enrichment_runs
        SET state = 'complete', completed_at = ?, final_url = ?, page_hash = ?,
            score = ?, temperature = ?
        WHERE id = ? AND state = 'running'
        """,
        (
            NOW.isoformat(),
            source,
            "e" * 64,
            result.score,
            result.temperature,
            run_id,
        ),
    )

    qc = run_qc(conn, firm_id=firm_id, now=NOW)

    assert not qc.passed
    assert {"FCA_EVIDENCE_STALE", "DUPLICATE_WEBSITE", "CONTRADICTORY_EVIDENCE"} <= set(
        qc.reasons
    )
    with pytest.raises(ValueError, match="passing current QC"):
        review_firm(
            conn,
            firm_id=firm_id,
            decision="approved",
            qc_run_id=qc.qc_run_id,
            notes=None,
            rejection_reason=None,
            now=NOW,
        )


def test_approved_qc_is_revalidated_when_companies_house_receipt_becomes_stale(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    firm_id = _stage(conn)
    _approve_current_firm(conn, firm_id)
    assert is_outreach_ready(conn, firm_id=firm_id, now=NOW)

    conn.execute(
        """
        UPDATE leads SET companies_house_verified_at = ?
        WHERE id = (SELECT lead_id FROM fca_firms WHERE id = ?)
        """,
        ((NOW - timedelta(days=31)).isoformat(), firm_id),
    )

    assert not is_outreach_ready(conn, firm_id=firm_id, now=NOW)


def test_approved_qc_is_revalidated_when_duplicate_website_appears(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    firm_id = _stage(conn)
    _approve_current_firm(conn, firm_id)

    _stage(conn, frn="234567")

    assert not is_outreach_ready(conn, firm_id=firm_id, now=NOW)


def test_approved_qc_is_revalidated_as_fca_evidence_ages(tmp_path):
    conn = connect_database(tmp_path / "govscout.sqlite3")
    migrate(conn)
    firm_id = _stage(conn, observed=NOW - timedelta(days=29))
    _approve_current_firm(conn, firm_id)
    assert is_outreach_ready(conn, firm_id=firm_id, now=NOW)

    assert not is_outreach_ready(conn, firm_id=firm_id, now=NOW + timedelta(days=2))
