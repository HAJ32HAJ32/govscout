from datetime import UTC, datetime

from govscout.web.evidence_copy import build_evidence_rows


def _item(signal_group, code, state, weight, *, source_url="https://example.test/", excerpt=None):
    return {
        "signal_group": signal_group,
        "code": code,
        "evidence_state": state,
        "weight": weight,
        "source_url": source_url,
        "excerpt": excerpt,
    }


def test_accountability_signals_combine_into_one_row_with_summed_weight():
    items = [
        _item("accountability", "FCA_REGULATED", "present", 40, excerpt="FCA status: Authorised"),
        _item("accountability", "ESTABLISHED_COMPANY", "present", 10, excerpt="Incorporated 2020-01-15"),
    ]
    rows = build_evidence_rows(items, now=datetime(2026, 8, 7, tzinfo=UTC))
    accountability_rows = [row for row in rows if row.order_bucket == "accountability"]
    assert len(accountability_rows) == 1
    row = accountability_rows[0]
    assert row.weight == 50
    assert "FCA authorised" in row.verdict
    assert "incorporated 2020" in row.verdict
    assert "6 years trading" in row.verdict


def test_repeated_not_found_items_roll_up_into_one_low_value_row():
    items = [
        _item("site_health", f"{key}_URL_NOT_FOUND", "present", 0, excerpt="Requested URL returned NOT_FOUND")
        for key in ("privacy", "careers", "policy")
    ]
    rows = build_evidence_rows(items)
    assert len(rows) == 1
    row = rows[0]
    assert row.order_bucket == "low_value"
    assert row.rolled_up_count == 3
    assert "3 pages returned 404 during scan, low impact" == row.verdict
    assert len(row.detail_rows) == 3


def test_evidence_rows_ordered_intent_then_accountability_then_infrastructure_then_low_value():
    items = [
        _item("site_health", "TECH_TOOLING_DETECTED", "present", 5, excerpt="Script loaded from Google Tag Manager"),
        _item("accountability", "FCA_REGULATED", "present", 40),
        _item("ai_exposure", "AI_VISIBLE", "present", 30),
        _item("site_health", "SOMETHING_URL_NOT_FOUND", "present", 0),
    ]
    rows = build_evidence_rows(items)
    buckets = [row.order_bucket for row in rows]
    assert buckets == ["intent", "accountability", "infrastructure", "low_value"]


def test_tech_tooling_verdict_reuses_the_stored_excerpt_tool_name():
    items = [_item("site_health", "TECH_TOOLING_DETECTED", "present", 5, excerpt="Script loaded from Google Tag Manager")]
    rows = build_evidence_rows(items)
    assert rows[0].verdict == "Google Tag Manager detected on homepage"


def test_unmapped_code_falls_back_to_humanized_signal_group_rather_than_dropping():
    items = [_item("governance_gap", "SOME_NEW_SIGNAL", "unknown", 0)]
    rows = build_evidence_rows(items)
    assert len(rows) == 1
    assert "Governance gap" in rows[0].verdict
    assert "Could not confirm" in rows[0].verdict
