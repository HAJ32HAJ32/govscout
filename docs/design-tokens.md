# `/today` design tokens

Reference for the token layer shipped in the visual pass (`docs/today-visual-pass.md`). Source of truth is `src/govscout/web/templates/_tokens.html` — this file documents *why* the values were chosen and records the contrast verification the visual-pass brief required. If the two ever disagree, the template file wins; update this doc to match.

The tokens are `:root` custom properties, `{% include %}`-ed as the first thing inside `today.html`'s single nonce-gated `<style>` tag (see the visual-pass brief's implementation notes for why this is inline rather than a linked stylesheet).

## Colour

All pairs verified against WCAG AA (4.5:1 for normal text) using the standard relative-luminance formula. Ratios below are exact, computed programmatically at implementation time (not estimated).

| Token(s) | Value | Paired against | Contrast |
|---|---|---|---|
| `--ink` | `#17251f` | `--surface-raised` `#ffffff` | 15.90:1 |
| `--ink-secondary` | `#47564e` | white | 7.75:1 |
| `--ink-muted` | `#5c6b63` | white | 5.62:1 |
| `--on-brand` (white text) | `#ffffff` | `--brand` `#154f3b` | 9.49:1 |
| `--temp-hot-text` | `#8a2f22` | `--temp-hot-bg` `#fbe6e2` | 6.98:1 |
| `--temp-warm-text` | `#7a5300` | `--temp-warm-bg` `#fbf0d9` | 6.06:1 |
| `--temp-cool-text` | `#2c4f66` | `--temp-cool-bg` `#e4edf3` | 7.32:1 |
| `--signal-intent-text` | `#5b3a8c` | `--signal-intent-bg` `#f1eaf9` | 7.36:1 |
| `--signal-good-text` | `#1f6b45` | `--signal-good-bg` `#e4f3ea` | 5.64:1 |
| `--signal-warn-text` | `#7a4a00` | `--signal-warn-bg` `#fdefd6` | 6.59:1 |
| `--signal-danger-text` | `#8a2020` | `--signal-danger-bg` `#fbe6e6` | 7.61:1 |

`--line` (`#e0e6e2`) and `--surface`/`--surface-raised`/`--brand`/`--brand-dark` are structural/background colours, not text-on-background pairs, and are exempted from the AA text check (used for borders, fills and gradients only — never as text colour on their own).

Role definitions (brief §3.1): `--brand` is reserved for the page header, the single primary button per card, and links — nowhere else, so "green" always means "the one thing to do next."

## Type scale

Four sizes only, each a token:

| Token | Size | Usage |
|---|---|---|
| `--text-title` | 20px/600 | Page title only |
| `--text-name` | 15px/600 | Firm names, candidate domains |
| `--text-body` | 14px/400 | Body copy, evidence verdicts, buttons, form labels' input text |
| `--text-meta` | 12.5px/400 | Metadata lines, weights, source links, section labels |

`--leading-body: 1.45`, `--leading-heading: 1.2`.

## Spacing and radius

4px scale: `--space-1` (4) `--space-2` (8) `--space-3` (12) `--space-4` (16) `--space-6` (24) `--space-8` (32), plus `--space-card: 20px` (card internal padding, a brief-specified value outside the generic step scale).

Radii: `--radius-card` 12px, `--radius-row` 8px, `--radius-control` 6px, `--radius-chip` 999px.

## Deferred

No icon set is vendored (see implementation notes in `docs/today-visual-pass.md`). Evidence-row icons are CSS-drawn dots coloured per signal type; the QC banner uses a plain unicode `⚠`. Revisit if a lightweight icon set becomes cheaply available.
