from govscout.web.app import classify_firm_state


def _row(*, enrichment_run_id=None, website_evidence_action=None):
    return {
        "enrichment_run_id": enrichment_run_id,
        "website_evidence_action": website_evidence_action,
    }


def test_scored_firm_is_state_c_regardless_of_qc_or_website_provenance():
    row = _row(enrichment_run_id=42, website_evidence_action=None)
    assert (
        classify_firm_state(row, has_candidate_search=False, candidate_search_enabled=True)
        == "C"
    )


def test_confirmed_website_awaiting_processing_is_state_b():
    row = _row(enrichment_run_id=None, website_evidence_action="assert")
    assert (
        classify_firm_state(row, has_candidate_search=False, candidate_search_enabled=True)
        == "B"
    )


def test_candidate_search_already_run_is_state_b():
    row = _row(enrichment_run_id=None, website_evidence_action=None)
    assert (
        classify_firm_state(row, has_candidate_search=True, candidate_search_enabled=True)
        == "B"
    )


def test_no_search_capability_is_state_b_even_without_a_search():
    row = _row(enrichment_run_id=None, website_evidence_action=None)
    assert (
        classify_firm_state(row, has_candidate_search=False, candidate_search_enabled=False)
        == "B"
    )


def test_nothing_yet_and_search_available_is_state_a():
    row = _row(enrichment_run_id=None, website_evidence_action=None)
    assert (
        classify_firm_state(row, has_candidate_search=False, candidate_search_enabled=True)
        == "A"
    )
