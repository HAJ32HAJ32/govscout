import json

from govscout.web.check_codes import translate_check_codes


def test_state_a_and_b_never_surface_check_codes():
    reasons = json.dumps(["SCAN_MISSING", "WEBSITE_MISSING"])
    assert translate_check_codes(reasons, "A") == []
    assert translate_check_codes(reasons, "B") == []


def test_state_c_surfaces_mapped_codes_with_plain_english_copy():
    reasons = json.dumps(["SCAN_MISSING"])
    warnings = translate_check_codes(reasons, "C")
    assert len(warnings) == 1
    assert warnings[0].code == "SCAN_MISSING"
    assert "scan did not complete" in warnings[0].message


def test_state_c_evidence_unknown_warns_before_approving():
    warnings = translate_check_codes(json.dumps(["EVIDENCE_UNKNOWN"]), "C")
    assert "Review before approving" in warnings[0].message


def test_unmapped_code_falls_back_to_generic_warning_rather_than_vanishing():
    warnings = translate_check_codes(json.dumps(["SOME_NEW_CODE"]), "C")
    assert len(warnings) == 1
    assert warnings[0].code == "SOME_NEW_CODE"
    assert "SOME_NEW_CODE" in warnings[0].message


def test_empty_or_missing_reasons_produce_no_warnings():
    assert translate_check_codes(None, "C") == []
    assert translate_check_codes("[]", "C") == []
    assert translate_check_codes("not json", "C") == []
