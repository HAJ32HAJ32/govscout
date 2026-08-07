# GovScout: /today visual pass brief

**Purpose of this document.** This is the reference brief for the presentation layer of the `/today` review queue. It is the companion to `docs/today-redesign.md`, which defined the structure (three states, evidence rows, check-code surfacing). That structure shipped at commit `d5b6289` and is correct. This brief changes how it looks. It should live at `docs/today-visual-pass.md` and be referenced from the README alongside the first brief.

**How to use this brief.** Run in plan mode first. Before planning, audit the current CSS approach (framework or hand-rolled, where styles live, what is inline in templates) and state it in the plan. The plan must map each section below to specific files. Wait for approval before implementing.

---

## 1. Problem statement

The redesign at `d5b6289` reorganised the page correctly but inherited the old presentation: near-uniform text sizing, full-width solid-green buttons for actions of very different importance, minimal spacing rhythm, and evidence rows that read as a flat list. The page is structurally right but not scannable. The operator should be able to read the whole queue in seconds and the expanded card in one glance, with the eye landing on decisions before detail.

## 2. Scope and non-goals

**In scope:** CSS, template markup classes and small structural wrappers needed for styling, a design-token layer, iconography if trivially available, responsive behaviour.

**Not in scope, do not touch:**

- State classification, evidence translation, check-code mapping, scoring, or any Python logic beyond passing existing values to templates.
- Copy changes. The plain-English verdicts from the first pass are final for now.
- New features of any kind, including LinkedIn, filters, sorting controls, or bulk actions.
- JavaScript beyond what already exists for expand/collapse and form disclosure. If a styling goal appears to need new JS, flag it in the plan.

If any change in this brief appears to require touching the not-in-scope list, stop and flag it.

## 3. Design tokens (the foundation, do this first)

Create one tokens layer (CSS custom properties in a single file, for example `static/tokens.css`, imported before all other styles). All component styles must reference tokens, never raw values. Migrate existing styles to tokens as they are touched; do not leave a parallel system.

### 3.1 Colour roles

GovScout keeps its existing dark-green identity but colours must become roles, not decoration:

- `--ink`: primary text, near-black.
- `--ink-secondary`: metadata, roughly 60 per cent strength.
- `--ink-muted`: timestamps, weights, tertiary detail.
- `--surface`: page background, warm off-white.
- `--surface-raised`: card and row background, white.
- `--line`: hairline borders, very light.
- `--brand`: the existing dark green. Reserved for: the page header, the single primary button per card, and links. Nowhere else. The current page uses solid green for six different buttons per card; after this pass, green means "the one thing to do next".
- `--temp-hot`, `--temp-warm`, `--temp-cool`: backgrounds and text pairs for the temperature chip (suggested: soft red/amber/slate pairs at low saturation, dark text of the same hue).
- `--signal-intent`: purple family pair, for governance-gap and AI-mention evidence.
- `--signal-good`: green family pair (distinct from `--brand`, lighter), for accountability evidence.
- `--signal-warn`: amber pair, for QC banners.
- `--signal-danger`: red pair, reserved for destructive confirmation states only.

Every colour pair must meet WCAG AA contrast (4.5:1 for text). State the checked ratios in the plan.

### 3.2 Type scale

Maximum four sizes on the page, from a fixed scale:

- 20px/600: page title only.
- 15px/600: firm names.
- 14px/400: body, evidence verdicts, buttons, form labels.
- 12.5px/400: metadata lines, weights, source links, section labels.

Section labels ("Ready to review · 1") render in 12.5px, 500 weight, letter-spaced 0.04em, `--ink-secondary`. No other uppercase or letter-spaced text anywhere.

Line height 1.45 for body, 1.2 for headings. If the current stack has no font loaded, system UI stack is fine; do not add a webfont in this pass.

### 3.3 Spacing and radius

Spacing on a 4px scale: 4, 8, 12, 16, 24, 32. Card internal padding 20px. Gap between cards 12px. Gap between queue sections 32px. Radius: 12px cards, 8px rows within grouped lists, 6px buttons and inputs, 999px chips. No other values.

## 4. Component specifications

### 4.1 Buttons, the most important fix on the page

Three tiers, and every action on the page must be assigned to exactly one:

- **Primary** (`--brand` background, white text, 6px radius, padding 8px 16px, intrinsic width, never full-width): exactly one per card maximum. State C: `Approve for outreach`. State B expanded: the confirm button on the top-ranked genuine candidate only. State A: `Find likely websites`.
- **Secondary** (transparent background, 1px `--line` border, `--ink` text, same padding): `Reject`, `Review candidates`, confirm buttons on non-top candidates, `Enter website manually` once opened.
- **Quiet** (no border, no background, `--ink-secondary` text, underline on hover): `Add contact`, `Withdraw website`, `Archive`, all disclosure toggles.

Destructive actions (`Archive`, `Withdraw website`) stay quiet at rest; their confirmation step inside the opened form may use `--signal-danger`. The current pattern of a pink full-width Archive button permanently visible is removed everywhere.

### 4.2 Collapsed rows (states A and B)

Height roughly 56 to 64px. Single line layout: firm name (15px/600) with metadata line under it (12.5px, `--ink-muted`), actions right-aligned and vertically centred. Rows in a group share one bordered container with hairline separators between rows, not individual bordered cards. Hover: `--surface` tint on the row. The whole row is not a click target; only the buttons are, to prevent accidental expansion during scanning.

### 4.3 The State C card

- **Header:** name left, temperature chip right. Chip: 12.5px/600, coloured pair per band, format "Warm · 70". Metadata line under the name with interpuncts, links in `--brand`.
- **QC banner:** `--signal-warn` pair, 12px vertical padding, icon plus one sentence, sits directly under the header with 16px gap. Never taller than two lines.
- **Evidence rows:** each row is a 3-column grid: 20px icon column, verdict, right-aligned "+N · source" in `--ink-muted` 12.5px. Rows separated by hairlines, 10px vertical padding, no backgrounds. Intent rows get their icon in `--signal-intent` and the key phrase ("potential governance gap") in the same colour; the rest of the verdict stays `--ink`. Rolled-up rows render in `--ink-muted` with a chevron disclosure. The weights column must align vertically down the card so the score can be summed by eye.
- **Action bar:** top hairline, 16px padding above. Primary and `Reject` left, quiet links right with 16px gaps. On narrow screens the quiet links wrap below.

### 4.4 Candidate list (state B expanded)

Genuine candidates: domain in 15px/600 as the first line, snippet clamped to two lines in `--ink-secondary`, confirm button right-aligned. Demoted directory candidates: entire entry in `--ink-muted`, "Directory listing" label chip in front of the domain, confirm rendered as a quiet action, grouped after all genuine candidates under a hairline.

### 4.5 Forms on demand

Opened forms (contact capture, manual URL, withdraw, archive) render inside the card in a `--surface` inset panel, 8px radius, 16px padding, with a quiet `Cancel` that closes it. Inputs: 1px `--line` border, 6px radius, 8px 10px padding, focus ring in `--brand` at 30 per cent. Labels above inputs in 12.5px `--ink-secondary`. Never more than one form open per card; opening one closes another.

### 4.6 Page frame

Content column max-width 760px, centred, 24px side padding. The green page header shrinks to a slim band: title 20px, the review instruction line moves into the empty-state or a subtitle at 12.5px. Count line right-aligned in the header band or directly beneath it.

## 5. Responsive behaviour

One breakpoint at 640px. Below it: card padding drops to 16px, the action bar stacks (buttons row above quiet links row), evidence weight column moves under the verdict as a suffix rather than a third column, and collapsed-row actions become a single chevron affordance opening the row. Nothing horizontal-scrolls.

## 6. Documentation requirements

Same rules as the first brief, in the same piece of work as the code:

- This brief lands as `docs/today-visual-pass.md`, referenced from the README next to `docs/today-redesign.md`.
- Add a short `docs/design-tokens.md` (or a closing section in this file) listing the final token values actually shipped, so future work references the file rather than screenshots.
- Record resolved ambiguities in a closing "Implementation notes" section of this brief, following the pattern established in the first pass.
- Update any technical docs or comments that describe the old styling approach.
- Produce a ready-to-paste vault block for H: two or three sentences for `Current status` noting the visual pass, tokens layer, and single-primary-button rule, with the commit hash, plus a one-line decision-log entry pointing at this document.

## 7. Verification checklist

- [ ] Exactly one `--brand` primary button visible per card or row, page-wide.
- [ ] No full-width buttons anywhere.
- [ ] Four text sizes only; checked by searching the CSS for font-size declarations.
- [ ] All colours reference tokens; no raw hex values in component CSS or templates.
- [ ] Evidence weights align in a column and visibly sum to the chip score on the LMB card.
- [ ] Directory candidates are visually subordinate to genuine candidates on the LSK card.
- [ ] Collapsed rows are roughly 60px; five firms fit on one 1440x900 screen with room to spare.
- [ ] All text/background pairs pass AA contrast.
- [ ] Page renders correctly at 375px wide with no horizontal scroll.
- [ ] No Python logic changed; test count unchanged and passing.
- [ ] Docs and vault block produced.

## 8. Open questions for the plan, not to resolve unilaterally

- What the current CSS setup is (framework, single stylesheet, inline styles) and the cheapest honest migration path to the tokens layer.
- Whether an icon set is already available or trivially vendorable; if not, ship without icons rather than adding a dependency, and note it as deferred.
- Whether the existing expand/collapse JS supports the "one form open per card" rule or needs a small extension (flag if so).

---

## 9. Implementation notes (resolved during the build)

Shipped at commit range starting with the `script-src` CSP change and template rewrite following `d5b6289`. Decisions locked in so a later session doesn't have to re-derive them:

- **CSS setup audit**: no framework; one hand-rolled inline `<style nonce="{{ csp_nonce }}">` block in `today.html`. CSP's `style-src` is `'nonce-{nonce}'` only (no `'self'`), so a literal external `static/tokens.css` file would be blocked outright. Resolution: the tokens layer is a Jinja partial, `src/govscout/web/templates/_tokens.html` (a bare `:root { ... }` block, no selectors), `{% include %}`-ed as the first thing inside the existing nonce-gated `<style>` tag. This is genuinely "one file, imported before all other styles" — it's just inlined rather than linked, so CSP never needed loosening.
- **One-open-form-per-card required a small CSP change and a small script.** `security_headers` in `app.py` now also sets `script-src 'nonce-{nonce}'` (same per-request nonce already used for `style-src`), and `today.html` ships one ~15-line inline `<script nonce>` that closes sibling open `<details class="quiet-action">`/`<details class="row-toggle">` panels within the same card/row when one opens. This is the only Python file touched in this pass, and it's a security-header addition, not review-queue logic.
- **Icons**: no icon set is vendored anywhere in the repo (checked: no `static/` folder, no vendored SVG/webfont) and none was trivially available without adding a dependency. Shipped without one, per the brief's own fallback: evidence-row icons are small CSS-drawn dots colored per signal type; the QC banner uses a plain unicode `⚠`. Deferred, not forgotten — revisit if a real icon set becomes available cheaply.
- **Directory-candidate confirm re-exposed.** The structural pass (`d5b6289`) fully hid the confirm form for demoted directory candidates. This pass restores it as a quiet-tier action ("Confirm this website anyway") per §4.4's "confirm rendered as a quiet action" — the underlying endpoint was already unrestricted and firm-scoped, so this is template-only, not a new capability.
- **Row B click-target change.** §4.2's "only the buttons are click targets, never the whole row" required moving away from the structural pass's whole-row `<summary>` (name + meta + everything as one click target). Row B now renders name/meta as always-visible, non-interactive text, with a single `Review candidates` toggle (`<details class="row-toggle">`) as the only element that expands the row — a `flex-basis: 100%` trick makes the expanded content drop below the row rather than squeezing into the toggle's own flex column.
- **Mobile single-chevron collapse** (§5) uses `details > :not(summary) { display: block !important; }` above 640px to force `<details>` content to always render regardless of the native `open` attribute, and lets native collapse apply below the breakpoint — a well-established CSS-only pattern, no JS needed for this part.
- Final token values and their verified contrast ratios are in `docs/design-tokens.md`.
