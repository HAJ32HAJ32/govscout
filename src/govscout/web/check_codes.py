"""Translate raw QC check codes into plain-English, state-aware warnings.

See docs/today-redesign.md section 5: a check only surfaces as a warning
when it is news - i.e. it tells the operator something the firm's state does
not already imply. States A and B never surface check codes at all (a
missing website or scan is implied by being in that state). State C surfaces
every code, using mapped copy when available and a generic fallback
otherwise, so a new code is never silently dropped.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

# Copy for codes that are "news" once a firm has reached state C (scored,
# awaiting decision). Keep in sync with docs/today-redesign.md section 5.
_STATE_C_MESSAGES: dict[str, str] = {
    "WEBSITE_MISSING": (
        "Data inconsistency: this firm is scored but has no website evidence on record."
    ),
    "SCAN_MISSING": "The site scan did not complete. The score may be incomplete.",
    "EVIDENCE_UNKNOWN": "QC could not verify one piece of evidence. Review before approving.",
}


@dataclass(frozen=True, slots=True)
class CheckWarning:
    code: str
    message: str


def translate_check_codes(qc_reasons_json: str | None, state: str) -> list[CheckWarning]:
    """Return the warnings a firm's card should show for its pipeline state.

    ``qc_reasons_json`` is the raw JSON array stored in ``qc_runs.reason_codes``
    (e.g. ``'["SCAN_MISSING","WEBSITE_MISSING"]'``). Only state C firms ever
    surface warnings - states A and B render nothing, since every code that
    could fire there is already implied by the collapsed-row summary.
    """
    if state != "C" or not qc_reasons_json:
        return []
    try:
        codes = json.loads(qc_reasons_json)
    except (TypeError, ValueError):
        return []
    if not isinstance(codes, list):
        return []
    warnings: list[CheckWarning] = []
    for code in codes:
        if not isinstance(code, str) or not code:
            continue
        message = _STATE_C_MESSAGES.get(code, f"{code}: needs review before approving.")
        warnings.append(CheckWarning(code=code, message=message))
    return warnings
