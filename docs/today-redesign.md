# GovScout: /today redesign brief

**Purpose of this document.** This is the reference brief for restructuring the `/today` review queue. It should live in the GovScout repository (suggested: `docs/today-redesign.md`) so future sessions can check decisions against it. Read it fully before planning. This is a reorganisation of what already exists, not a feature build.

**How to use this brief.** Run in plan mode first. Produce a plan that maps each section below to specific files and changes, flag anything in the current codebase that contradicts this brief, and wait for approval before implementing.

---

## 1. Problem statement

The current `/today` page renders every firm as an identical full-height card regardless of pipeline state. Every card shows every form permanently, every piece of evidence at equal weight, raw scraped text, and raw check codes (for example `["SCAN_MISSING","WEBSITE_MISSING"]`). Five firms fill several screens. There is no approve or reject action visible, even though human review is the entire point of the page.

The goal: H opens `/today`, sees at a glance what each firm needs from him, spends his attention on scored firms awaiting a decision, and can audit any score against plain-English evidence with sources. Confidence in the data is the product.

---

## 2. Non-goals (do not build these)

- No LinkedIn discovery or enrichment.
- No autonomous outreach, email drafting, or send routes.
- No changes to scoring weights, thresholds, or the QC logic itself.
- No changes to evidence gating. Contact capture stays manual and evidence-gated exactly as shipped in commit `1569828`.
- No new data collection. Everything on screen must come from data the pipeline already stores.
- No dark mode, theming, or brand refresh work in this pass.

If implementing anything in this brief appears to require one of the above, stop and flag it in the plan rather than proceeding.

---

## 3. The three pipeline states

Every firm on `/today` is in exactly one of three states. The card is state-aware: each state renders a different layout and asks the operator exactly one primary question.

### State A: Not yet researched

Companies House verified, no website on record, no candidate search run yet.

**Primary question:** start research or skip?

**Renders as:** a collapsed row. Firm name, FRN, town, one-line status ("Companies House verified, no website on record"), two actions: `Find likely websites` (primary) and `Archive` (quiet). No forms. No evidence section. Target height: one row, roughly 60px.

### State B: Needs a website confirmed

Candidate search has run (or manual entry is available). Awaiting operator confirmation of the official website.

**Primary question:** which of these is their site?

**Renders as:** a collapsed row by default. Firm name, FRN, town, one-line summary of candidates found (for example "1 likely site found: lskinsurance.co.uk" or "No plausible sites found, manual entry available"), one action: `Review candidates`.

Expanding the row reveals the candidate list. Each candidate shows: the domain (prominent), a shortened snippet (max two lines, truncated), and a confirm button. The manual URL entry form sits below the candidates, collapsed behind a `Enter website manually` link.

**Candidate quality:** known directory and data-aggregator domains (Tracxn, YouControl, Endole, Companies House mirrors and similar) must be either filtered out before display or visually demoted with a "Directory listing, unlikely to be the official site" label and no prominent confirm button. Maintain the domain list as a constant in code so it can grow. This is the one pipeline-adjacent change in this brief; keep it to a display-layer or pre-display filter, do not change what is stored.

### State C: Scored, awaiting decision

Website confirmed, evidence gathered, QC run, score assigned.

**Primary question:** reach out or not?

**Renders as:** the full card. This is the only state that gets one. Anatomy, top to bottom:

1. **Header.** Firm name, then one metadata line: FRN, FCA status, town, link to FCA Register, link to the confirmed website. Right-aligned: the score chip.
2. **QC banner** (conditional, see section 5). Only when there is a check the operator must act on.
3. **Evidence list** (see section 4).
4. **Action bar.** Left: `Approve for outreach` (the single primary button on the card) and `Reject`. Right, as quiet text links: `Add contact`, `Withdraw website`, `Archive`. Each quiet link opens its existing form inline on demand. The forms themselves are unchanged; only their disclosure changes.

**Approve and Reject** must connect to the review actions in the existing data model (approve, reject, needs more research). If a review-state field or endpoint does not yet exist, flag it in the plan; it is in scope to add the minimal write path, since human review is the documented purpose of the page.

---

## 4. Evidence row anatomy

Every evidence item in a State C card renders with the same structure:

```
[icon]  [plain-English verdict]                    [+weight] · [source link]
```

Rules:

- **Verdict, not raw material.** Each signal gets a one-line plain-English verdict. Raw scraped excerpts (page text, menus, phone numbers) never appear at the top level. Where a raw excerpt exists and is useful, it sits behind an expandable disclosure on that row.
- **Weight column.** Show the score contribution of each signal (+40, +30, +15, +10, +5, +0) so the total score can be audited line by line. Weights come from the scoring code; do not hardcode them in the template. If a signal contributed nothing, show +0 rather than hiding it.
- **Source link on every row.** No verdict without a source.
- **Roll up repetition.** Multiple evidence items of the same type and outcome (for example several "Requested URL returned NOT_FOUND" entries) collapse into one row: "3 pages returned 404 during scan, low impact", expandable to the individual entries.
- **Order by decision value**, not by capture order: intent signals first (AI mentions, policy silence), then accountability (FCA status, incorporation age), then infrastructure (tooling detected), then rolled-up low-value items last.

**Signal framing.** These verdicts and treatments are fixed decisions, do not soften them:

| Signal outcome | Verdict copy | Treatment |
|---|---|---|
| Privacy/policy pages found, no AI mention | "Privacy policy found, silent on AI use · potential governance gap" | Intent colour (purple family). This is a positive outreach signal, not an absence. |
| AI mentioned on site | "AI mentioned on site · N mentions" with page context if stored | Intent colour. |
| No AI mentions anywhere on site | "No AI mentions on site" | Neutral grey. Informational, not negative. |
| FCA authorised + incorporation age | "FCA authorised · incorporated YYYY, N years trading" | Success colour. Combine into one row. |
| Analytics/chat tooling detected | Name the tool: "Google Tag Manager detected on homepage" | Neutral grey. |
| Scan 404s and similar | Rolled up, "low impact" | Muted, collapsed. |

---

## 5. Check codes: what surfaces and what does not

The current UI shows raw arrays like `["SCAN_MISSING","WEBSITE_MISSING"]` under "Why the checks need attention", on every card. The rule going forward:

**A check only surfaces as a warning when it is news, meaning it tells the operator something the firm's state does not already imply.**

| Code | State A | State B | State C |
|---|---|---|---|
| `WEBSITE_MISSING` | Implied by state, never shown | Implied by state, never shown | Should not occur; if it does, show as a warning (data inconsistency) |
| `SCAN_MISSING` | Implied, never shown | Implied, never shown | Warning: "The site scan did not complete. The score may be incomplete." |
| `EVIDENCE_UNKNOWN` | Not applicable | Not applicable | Warning banner: "QC could not verify one piece of evidence. Review before approving." |
| Any QC fail | Not applicable | Not applicable | Warning banner, plain English, stating what failed and what it blocks |

Maintain a single translation map from check code to (display rule per state, English copy) in one place in code. Any code not in the map renders its raw value inside a generic warning rather than being silently dropped, so new codes are never invisible.

Remove the "Research state: Checks need attention" line entirely. The queue grouping (section 6) replaces it.

Contradictory strings like "Reprocessing: succeeded · QC_FAIL" must never render as-is. If reprocessing metadata is useful, it belongs behind a disclosure in plain English.

---

## 6. Page structure

`/today` becomes a grouped queue:

1. **Ready to review** (State C firms), sorted by score descending. Full cards.
2. **Needs a website confirmed** (State B firms). Collapsed rows.
3. **Not yet researched** (State A firms). Collapsed rows.

Page header: a compact count line ("5 firms · 1 scored · 4 in research"). Empty groups are hidden, not shown with placeholder text. If nothing is ready to review, the page says so plainly at the top of the queue rather than burying it at the bottom.

Score labels use temperature language matching the scoring model: **Hot** (75 and above), **Warm** (55 to 74), **Cool** (below 55), each with the numeric score, for example "Warm · 70". Remove "Medium priority" and similar priority language everywhere it appears, including any API responses or templates that feed the UI.

Companies House verification history moves behind a disclosure on the card ("Verified against Companies House on [date]" as the summary line). Raw timestamps with microseconds must not appear; format dates as "7 Aug 2026".

---

## 7. Documentation requirements

This is not optional and must be part of the implementation plan, not an afterthought. The project has a deliberate split: the repository is the technical truth, the vault note (`GovScout.md`) is the founder-facing truth. Both must be updated in the same piece of work as the code, so legacy documentation never describes a UI that no longer exists.

**In the repository:**

- Add this brief as `docs/today-redesign.md` (or the repo's existing docs convention) and reference it from the main README or technical docs index.
- Update any existing technical documentation, screenshots, or route descriptions that describe the old `/today` layout. Search the repo for references to "Checks need attention", "Medium priority", and the old card structure, and update or remove them.
- Update or add tests covering: state classification (a firm resolves to exactly one of A/B/C), the check-code translation map (including the unknown-code fallback), evidence roll-up, and the score-band labels.
- Record the change in whatever changelog or commit convention the repo uses, with a summary of the three-state model.

**In the vault (provide as a ready-to-paste block for H, since the agent must not write to the vault directly if that boundary applies):**

- A short update for the `Current status` section of `GovScout.md`: the `/today` redesign, the three-state model, temperature labels replacing priority language, and the check-code surfacing rule.
- A one-line addition to the decision log pointing at `docs/today-redesign.md` as the reference for the redesign decisions.

**Terminology consistency:** after this change, the terms are "state" (A/B/C as named in section 3), "temperature" (hot/warm/cool), and "evidence row". Retire "priority" and "checks need attention" from code identifiers where cheap to do so; where renaming identifiers is risky, keep the identifier but ensure no user-facing string uses the old language, and note the mismatch in the technical docs.

---

## 8. Verification checklist

The plan must end with these checks:

- [ ] Each of the five current firms renders in exactly one group, with the correct layout for its state.
- [ ] LMB Insurance (State C) shows: temperature chip "Warm · 70", an `EVIDENCE_UNKNOWN` banner in plain English, an evidence list whose weights visibly sum to 70, one primary Approve button, and no permanently visible forms.
- [ ] LSK Risk Management (State B) shows one row; expanding it shows the genuine candidate prominently and directory listings demoted or filtered.
- [ ] Risk Kitchen (State A) is a single row with two actions.
- [ ] No raw check-code arrays, no "Medium priority", no "Checks need attention", no raw scraped page text at top level, no microsecond timestamps anywhere on the page.
- [ ] Approve and Reject write to the data model and the firm leaves the "Ready to review" group accordingly.
- [ ] Repository docs updated; vault update block produced for H.
- [ ] Existing behaviour unchanged: evidence gating, scoring weights, QC logic, no-autonomous-send.

---

## 9. Open questions to raise in the plan, not to resolve unilaterally

- Whether a review-state write path (approve/reject) already exists or needs adding, and the minimal shape of it.
- Whether the directory-domain filter should apply at candidate-storage time or display time (display time is the default assumption).
- Any places where the current templates make the three-state classification ambiguous (for example a firm with a confirmed website but a failed scan), and which state such firms should resolve to.

---

## 10. Implementation notes (resolved during the build)

The redesign shipped against this brief with the following decisions locked in, so a later session doesn't have to re-derive them by reading `classify_firm_state` cold:

- **Review write path**: already existed (`POST /today/review/<id>` → `quality.review_firm`). No new endpoint was needed.
- **Directory domains**: demoted in the UI (muted label, no confirm button), not filtered out of the candidate list, so an operator can still see and audit what the search actually returned. Classification is display-layer only (`website_candidates.is_directory_domain`), applied when building `/today`'s response, not at storage time.
- **State C threshold**: a firm is state C once it has an enrichment run at all (`enrichment_run_id is not None`) - regardless of whether the most recent QC run is current, stale, failed, or missing entirely. This is what section 5's own `SCAN_MISSING` row implies (a state-C warning, not a reclassification), and it's what the existing test suite's fixtures actually exercise. Any QC problem surfaces as a banner on the card; it never demotes a scored firm back to state A/B.
- **State A vs. B boundary**: state A requires an automated candidate search to actually be available and not yet run. If no search provider is configured, or a search has already run, or the website is already asserted but enrichment hasn't caught up yet, the firm is state B.
- Implemented in `src/govscout/web/app.py` (`classify_firm_state`, the `/today` handler), `src/govscout/web/check_codes.py`, `src/govscout/web/evidence_copy.py`, and the three `_firm_row_a/_firm_row_b/_firm_card_c` templates.
