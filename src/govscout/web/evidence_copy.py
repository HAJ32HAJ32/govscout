"""Turn raw ``evidence_items`` rows into plain-English, weighted, sourced rows.

See docs/today-redesign.md section 4 (evidence row anatomy) and its signal
framing table. Every evidence item becomes one ``EvidenceRow`` with a
one-line verdict, its score weight, a source link, and an optional raw
excerpt kept behind a disclosure. Repeated low-value site-health items
(404s, unscanned pages) roll up into a single row. Rows are ordered by
decision value: intent, then accountability, then infrastructure, then
rolled-up low-value items last.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_ORDER = {"intent": 0, "accountability": 1, "infrastructure": 2, "low_value": 3}

_STATE_WORD = {"present": "Found", "absent": "Not found", "unknown": "Could not confirm"}


@dataclass(frozen=True, slots=True)
class EvidenceRow:
    verdict: str
    color_class: str
    weight: int
    source_url: str | None
    order_bucket: str
    raw_excerpt: str | None = None
    rolled_up_count: int = 1
    detail_rows: tuple[dict, ...] = field(default_factory=tuple)


def _humanize(text: str) -> str:
    return text.replace("_", " ").strip().capitalize()


def _extract_tool(excerpt: str | None) -> str | None:
    if excerpt and excerpt.startswith("Script loaded from "):
        return excerpt[len("Script loaded from ") :]
    return None


def _parse_incorporation_year(excerpt: str | None) -> int | None:
    if not excerpt:
        return None
    match = re.search(r"(\d{4})-\d{2}-\d{2}", excerpt)
    return int(match.group(1)) if match else None


def _row_for(item: dict) -> EvidenceRow:
    code = item["code"]
    state = item["evidence_state"]
    weight = item["weight"] or 0
    source_url = item["source_url"]
    excerpt = item.get("excerpt")

    if code == "AI_VISIBLE":
        if state == "present":
            return EvidenceRow("AI mentioned on site", "intent", weight, source_url, "intent", excerpt)
        return EvidenceRow("No AI mentions on site", "neutral", weight, source_url, "intent", excerpt)

    if code == "PRIVACY_SILENT_ON_AI":
        if state == "present":
            return EvidenceRow(
                "Privacy policy found, silent on AI use · potential governance gap",
                "intent",
                weight,
                source_url,
                "intent",
                excerpt,
            )
        return EvidenceRow(
            "Privacy policy addresses AI use", "neutral", weight, source_url, "intent", excerpt
        )

    if code == "AI_POLICY_STATUS":
        verdict = "AI policy page found" if state == "present" else "No AI policy page found"
        return EvidenceRow(verdict, "neutral", weight, source_url, "intent", excerpt)

    if code == "TECH_TOOLING_DETECTED":
        if state == "present":
            tool = _extract_tool(excerpt) or "Analytics or chat tooling"
            return EvidenceRow(
                f"{tool} detected on homepage", "neutral", weight, source_url, "infrastructure", excerpt
            )
        return EvidenceRow(
            "No analytics or chat tooling detected",
            "neutral",
            weight,
            source_url,
            "infrastructure",
            excerpt,
        )

    # Fallback: never silently drop a signal, even one this table doesn't know about yet.
    label = _humanize(item["signal_group"])
    verdict = f"{label}: {_STATE_WORD.get(state, state)}"
    return EvidenceRow(verdict, "neutral", weight, source_url, "low_value", excerpt)


def _accountability_row(items: list[dict], *, now) -> EvidenceRow | None:
    fca = next((item for item in items if item["code"] == "FCA_REGULATED"), None)
    if fca is None:
        return None
    established = next((item for item in items if item["code"] == "ESTABLISHED_COMPANY"), None)
    weight = (fca["weight"] or 0) + (established["weight"] or 0 if established else 0)
    verdict = "FCA authorised"
    if established is not None and established["evidence_state"] == "present":
        year = _parse_incorporation_year(established.get("excerpt"))
        if year is not None:
            verdict = f"FCA authorised · incorporated {year}"
            if now is not None:
                years_trading = max(now.year - year, 0)
                verdict = f"{verdict}, {years_trading} years trading"
    return EvidenceRow(verdict, "success", weight, fca["source_url"], "accountability", fca.get("excerpt"))


def _rollup_row(items: list[dict], *, verb: str, noun: str) -> EvidenceRow:
    count = len(items)
    unit = noun if count != 1 else noun[:-1]
    verdict = f"{count} {unit} {verb}, low impact"
    total_weight = sum(item["weight"] or 0 for item in items)
    detail_rows = tuple(
        {"code": item["code"], "source_url": item["source_url"], "excerpt": item.get("excerpt")}
        for item in items
    )
    return EvidenceRow(
        verdict, "muted", total_weight, None, "low_value", None, rolled_up_count=count, detail_rows=detail_rows
    )


def build_evidence_rows(evidence_items: list[dict], *, now=None) -> list[EvidenceRow]:
    """Build display-ready evidence rows, combined/rolled-up and ordered.

    ``now`` is optional and only used to compute "N years trading" on the
    combined accountability row; omit it to render without that detail.
    """
    items = list(evidence_items)
    accountability = _accountability_row(items, now=now)
    items = [item for item in items if item["code"] not in ("FCA_REGULATED", "ESTABLISHED_COMPANY")]

    not_found = [item for item in items if item["code"].endswith("_URL_NOT_FOUND")]
    scan_status = [item for item in items if item["code"].endswith("_SCAN_STATUS")]
    remaining = [item for item in items if item not in not_found and item not in scan_status]

    rows: list[EvidenceRow] = []
    if accountability is not None:
        rows.append(accountability)
    rows.extend(_row_for(item) for item in remaining)
    if not_found:
        rows.append(_rollup_row(not_found, verb="returned 404 during scan", noun="pages"))
    if scan_status:
        rows.append(_rollup_row(scan_status, verb="could not be scanned", noun="pages"))

    rows.sort(key=lambda row: _ORDER[row.order_bucket])
    return rows
