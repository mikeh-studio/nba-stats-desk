# Ask workspace design QA

## Latest review: two-player scorecard (2026-09-12)

Source visual truth: `/Users/mikeh/.codex/generated_images/01a0980c-012a-7392-ad43-f83c85175b19/exec-82fe664d-e664-4c49-835c-90ff917cdea8.png`.
Implementation: http://127.0.0.1:8001/ask.
Evidence directory: `/private/tmp/nba-legacy-retirement-rollout/local_notes/comparison_ui_qa/`.

### Comparison evidence and state

- Source pixels: 935 x 1683. Desktop browser screenshot: 1265 x 3044. Narrow browser screenshot: 744 x 3908. Full-page browser surfaces were captured; CSS height/device density were not independently queried. The generated source does not specify an authoritative CSS viewport.
- `comparison-final.png`: source and final implementation together, normalized to 935 pixels wide each. This compares layout/hierarchy, not pixel-perfect equal-state rendering.
- `table-comparison.png`: source and implementation table regions together at equal normalized widths, inspected for labels, alignment, meters and readable text.
- `desktop-final.png` and `narrow-final.png`: final full-page captures. `impact-unavailable.png`: browser-tested missing net-rating state. `desktop-before.png`: pre-polish comparison.
- State: original Brunson vs Wemby question, 2025-26 playoffs, current source data through June 13, 2026, PTS selected. A second question is present, so the live UI includes the existing question selector. The source uses illustrative numbers and a populated impact chart; the implementation uses actual governed warehouse values and unavailable impact fields. This is an explicit data-coverage deviation, not a claim of complete advanced-metric integration.

### Findings, fixes and final review

- [P2, fixed] Repeated cohort counts and rounded differences obscured the shared comparison. Consolidated equal cohorts into one count, rounded accessible meter values consistently, and explained that edges use unrounded averages. Final table comparison verifies the clearer result.
- [P2, fixed] Scope text crowded the bottom of the headshot row. Added space below the two identities; final desktop and narrow captures verify separation.
- [P2, fixed] Narrow tables needed discoverable horizontal navigation. Added keyboard-focusable scroll regions, visible focus styling and a horizontal-scroll hint. The wide comparison remains intact rather than compressing six columns into unreadable text.
- No actionable P0/P1/P2 visual issues remain for the inspected available-data/missing-data states. Completing Win Shares and on/off analytics remains separate unfinished data integration.

### Required fidelity surfaces

- Fonts: existing Barlow/Barlow Condensed families, editorial display headings and readable body text preserved. Real names are visible in full in the content; tab ellipsis is intentional.
- Spacing/layout: dual identities, overall analysis, three icon-led takeaways, mirrored core table, separate impact section, chart and scoped composer follow the selected design. The page scrolls; longer real coverage explanations and question navigation account for extra height. Duplicate table headshots and a repeated large comparison title are omitted to reduce repetition.
- Colors: charcoal/vermilion retained; cool blue consistently identifies the second player's data. Named edges and numeric labels avoid color-only interpretation.
- Images/icons: real existing NBA headshots replace generated mock portraits. Vendored licensed Tabler icons and local fonts are reused; no generated faces or fabricated stat artwork are shipped.
- Copy: actual data replaces fixtures. The overall answer explicitly withholds an overall winning-impact verdict. Win Shares, WS/48 and on/off fields explain their missing inputs; no custom score or raw-plus/minus substitute is used. Source provenance and definitions are inspectable.

### Verification and boundaries

- Browser-tested the exact requested question, nickname resolution, shared playoff scope, PTS/AST switching, unavailable net-rating state, fill-only playmaking suggestion, same-chat submission, AST default on that follow-up, reload restoration and original-question selection.
- 184 focused Python tests and 37 JavaScript tests passed; the final history change also passed its 84-test focused subset. Ruff lint/format and diff checks passed.
- Tests cover missing/zero data, source-backed percentiles, ambiguous identities, retained sequential choices, explicit playoff years, unsupported-scope guards and frozen comparison context. No model call is needed for the explicit two-player scorecard route.
- No dedicated phone-width, touch, screen-reader or browser-console inspection was performed. Screenshots do not certify accessibility. Full Python suite remains unvalidated in the minimal runtime because pipeline dependencies were previously missing.
- The populated net-rating graphic from the prototype is not implemented as a fabricated live graphic. Its selector exposes the unavailable-data explanation; core-stat charts are functional. A verified advanced source is required before that feature can be completed.

Implementation checklist: selected layout applied; live core evidence connected; unavailable advanced state explicit; primary browser interactions tested; source/render comparisons inspected; local app left running; no commit, push or deployment.

final result: passed

---

## Earlier single-player review

Date: 2026-09-12

## Target and evidence

- Source visual truth: `/Users/mikeh/.codex/generated_images/01a0980c-012a-7392-ad43-f83c85175b19/exec-2c89ae94-b3f1-4e83-b320-06c88c036572.png`.
- Local implementation: http://127.0.0.1:8001/ask.
- Evidence directory: `/private/tmp/nba-legacy-retirement-rollout/local_notes/ask_ui_qa/` (local, ignored).
- Source: 946 x 1663 pixels. Desktop implementation: 1265 x 2483 pixels, full-page capture. Narrow implementation: 698 pixels wide, full-page capture. Browser CSS viewport height and device scale factor were not independently queried; no 1:1 CSS-pixel fidelity claim is made.
- Full-view comparison: `comparison-final.png`, source and implementation together, each normalized to 946 pixels wide. Implementation normalized height is 1857 pixels. The generated reference has no authoritative CSS viewport; this is a composition comparison, not a pixel-diff threshold.
- Focused comparison: `tabs-comparison.png` shows the source strip and rendered strip together. `narrow-chart-detail.png` verifies legible labels and complete edge dates after correction.
- Final rendered captures: `desktop-final.png`, `narrow-final.png`; searchable-history state: `history.png`.
- State: dark theme, 2025-26, Jalen Johnson, Sep 13 2025 through Sep 12 2026, combined regular season and playoffs, 78 appearances, AST selected. Four actual saved chats replace the mock's five fictional examples. The active chat contains a follow-up, so the implementation includes a question selector absent from the single-turn mock.

## Findings and comparison history

1. Initial desktop comparison (`desktop-before.png`): repeated cohort notes, redundant profile details and restored-status text made the hierarchy busier than the approved reference (P2). Consolidated the cohort note, tightened the player header, hid successful/restored status, and retained methodology in a disclosure. Post-fix: `desktop-after.png` and the final combined comparison.
2. Follow-up comparison: body/table type was too small relative to the editorial hierarchy (P2). Increased both to 1.15rem. Post-fix: `desktop-final.png` and `comparison-final.png`.
3. Narrow review: scaling the SVG reduced label readability (P2). Increased narrow-screen axis/value type, set a 640px minimum chart width with local horizontal scrolling, and anchored edge dates inward to avoid clipping. Post-fix: `narrow-final.png` and `narrow-chart-detail.png`. Final desktop comparison reconfirmed that this responsive correction preserved the desktop composition.

No actionable P0/P1/P2 visual findings remain in the inspected states.

## Required fidelity surfaces

- Typography: existing local Barlow and Barlow Condensed preserve the source's editorial hierarchy. Tab ellipsis is intentional; full opening questions remain in tooltips and history. Narrow chart labels were corrected as above.
- Spacing/layout: flat, scrollable page; overall analysis and three takeaways form desktop columns and stack at narrow widths. Metrics precede a large chart and contextual composer. Extra vertical space for real question navigation and evidence caveats is an intentional functional deviation.
- Colors/tokens: existing charcoal surfaces, vermilion active controls, muted rules and signed green/red changes retained. Signed numbers ensure change is not color-only. No gradient cards or new decorative UI system.
- Images/assets: real existing player headshot replaces the mock's generated portrait. Existing wordmark typography is retained. Consistent licensed Tabler icons replace illustrative mock icons; no newly drawn logo or fake player image.
- Copy/content: real saved evidence supplies all five statistics, percentiles, explicit comparison dates and monthly values. Mock-only claims/examples are not embedded. Minutes, shooting efficiency, turnovers and opponent defensive rating remain explicitly unevaluated. Only three supported takeaways are exposed, not six fixed area cards.

## Interaction checks

- Browser-tested: new chat, restore, More search and empty search, close without deleting, history reopen, reload persistence, fill-only suggestions, same-chat regular-season follow-up, older-question selection restoring its matching table/chart, and PTS/AST chart switching.
- A live follow-up changed combined 78 appearances to regular-season-only 72; selecting the original answer restored 78 and its 22.3 points average. During requests, navigation and season selection were visibly disabled.
- Unit tests cover five-tab overflow, non-destructive closing, search including follow-ups, history pagination beyond 25 conversations, preservation beyond 20 turns, saved-context rehydration and date freezing.
- 177 focused Python tests and 34 JavaScript tests passed. Ruff lint/format and diff whitespace checks passed. Focused mypy check reported no issues in three source files.
- Full Python suite collection is blocked in the minimal local app runtime by missing pandas in seven pipeline/model test modules. This is not a full-suite pass.

## Test gaps and accepted deviations

- Browser console errors were not separately inspected; screenshots and DOM/interaction checks are not full accessibility certification.
- No dedicated 390px phone or touch-device check was performed. 698px and 1265px browser surfaces were inspected. The five-to-six transition was unit-tested, not populated with six new live chats.
- ISO month labels, native percentile meters, real historical titles and the existing offseason indicator are intentional app-native differences from the generated mock. Data freshness remains available in methodology instead of duplicated mock footer chrome.
- Opponent-rating ingestion and minutes/efficiency/turnover-driven explanations are separate analytical work, not implied by this UI implementation.

## Implementation checklist

- [x] Preserve original editorial design direction and scrollable page.
- [x] Implement browser-like conversation tabs and searchable More history.
- [x] Keep follow-ups scoped to the chat and preserve prior answers.
- [x] Compare source and implementation together and correct observed visual issues.
- [x] Leave the local app running for review; no deployment or PR created.

final result: passed
