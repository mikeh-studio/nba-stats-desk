# Johnson reference text — option 2

Source visual truth: /Users/mikeh/.codex/generated_images/01a0980c-012a-7392-ad43-f83c85175b19/exec-033c5528-3bb0-4a54-a9de-0cb224cf3b5f.png

Implementation screenshot: /private/tmp/johnson-text-only.png

State: saved six-player leaderboard on /ask, final table scrolled into view.
Viewport: 727 × 599 CSS pixels; browser screenshot 727 × 599 at 1x.
Source: 1706 × 922 raster mockup. Scope is the selected text-color treatment,
not a request to resize the existing table to the mockup canvas. Comparison
uses the corresponding six-row table region; surrounding page content and
the larger source typography scale are intentional existing-layout differences.

## Findings

No actionable P0/P1/P2 differences for the requested change.

- Typography: existing font, size, weight and row rhythm retained.
- Layout: no new padding, extra label, colored separator or background.
- Colors: all seven Johnson cells computed as rgb(255, 121, 104), matching
  the selected coral; transparent backgrounds, zero top border, no shadow.
- Assets: none added or required.
- Content: Johnson remains sixth displayed row with league rank 8, 7.7 assists,
  78 appearances, 98.7 percentile and zero reference gap. Reference label removed.

## Verification

Opened the saved chat through More and inspected the actual browser rendering.
Compared the source and implementation images in a single review tool response.
Full table provides the focused region evidence; broader page layout is unchanged.
Eleven JavaScript tests passed, including preservation of rank and removal of label.
No new statistical request was needed. Other viewport sizes were not retested.
One visual pass; no corrective iterations required.

final result: passed
