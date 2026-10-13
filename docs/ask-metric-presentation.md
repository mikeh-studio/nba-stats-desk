# Ask overview metrics and samples

The server owns `semantic_evidence.presentation`, version
`ask_metric_presentation/3`, including selected metric keys, sample notes,
question focus, limitations and ordered takeaways. The browser renders those
facts without selecting the largest percentile as the headline.

## Visible metrics

| Focus | Primary metrics |
| --- | --- |
| General | PTS, REB, AST, 3PM, STL, BLK, TOV, MIN, FG%, FT%, TS% |
| Scoring | PTS, 3PM, FGA, 3PA, MIN, FG%, 3P%, FT%, TS% |
| Playmaking | AST, TOV, PTS, REB, MIN, TS% |
| Rebounding | REB, MIN, AST, TOV, STL, BLK |
| Defensive activity | STL, BLK, MIN, REB, AST, TOV |
| Contribution beyond scoring | AST, TOV, REB, STL, BLK, MIN |

General overviews show eleven metrics, with shooting percentages last. Focused broad performance/profile questions
show six or nine; remaining calculated metrics are expandable. The main table uses four compact
columns with inline %/minutes and percentage-point changes. Sample details remain expandable.
Key Takeaways use up to three visible icon/headline/explanation rows; limited-data
labels remain visible beside affected values. Explicit metric,
venue, opponent, ranking and unsupported grain requests retain their existing
routes and scope guards. A focused overview is a presentation of whole-game
observations, not permission to substitute them for unavailable tracking data.
Current overview scopes support season and calendar windows, plus the documented
last-ten-appearance default for recent performance. Other unsupported appearance
windows never become a calendar window silently.

Rebounding and defensive activity have separate takeaways and explicit coverage
limits. Supporting metrics are labeled as context, not measures of the focus.
No offensive/defensive rebound split, screening, foul, contest, chance, assignment
or passing-tracking statistics are inferred from the basic box score.

## Samples and comparisons

Every metric retains observed games, valid games, missing component counts and
its aggregate components. Makes/attempts accompany FG%, 3P% and FT%; FGA and FTA
accompany TS%, whose denominator uses the published 0.44 approximation. Ratios
pool components across valid appearances. Zero attempts produce an unavailable
percentage, while a recorded zero count remains zero.

Presentation states are `unavailable`, `limited` and `descriptive`. Partial
components or fewer than five valid appearances are limited; descriptive is not
an assertion of statistical precision. The five-appearance floor reuses the
project count-comparison policy. Changes are withheld if either period falls
below the floor or has partial components; original aggregate components remain
inspectable. Baseline coverage and zero-denominator guards still apply.
Overview percentile policy `ask_overview_percentiles/1` ranks PTS/REB/AST/STL/BLK,
3PM, TOV and MIN with the existing five-valid-appearance floor, complete metric
components and at least two qualified players. TOV reverses the direction: higher
percentiles mean fewer turnovers, with role/minutes context. MIN describes exposure
and uses a neutral bar. Other context rates remain unranked in this overview.

Shooting ranks pool unrounded aggregate components and use provisional project
minimums in the exact requested window: 100 FGA for FG%/TS%/eFG%, 50 FTA for FT%,
and 50 3PA for 3P%. These are not NBA official leader qualification, significance
or precision guarantees. Eligible peer cohorts are metric-specific, in the same
dates and phases, and exclude incomplete or insufficient samples. Ties share the
percentage of other qualified players strictly worse, divided by cohort size
minus one. Zero denominators never become zero-percent ranks.

Evidence retains `percentile_status`, label, cohort size, explanation and the
versioned qualification policy. Missing ranks show No data, Incomplete data,
Low sample, Low attempts, Few peers or Not ranked. Details disclose thresholds and
cohort sizes. Attempt eligibility does not change raw averages or existing
comparison suppression. Short windows may show percentages without percentile
ranks. Threshold coverage is inspected against frozen snapshots; no claim of
population-wide live-source validation is implied.

## Takeaways

Key Takeaways retain the original visible icon, headline and short explanation
structure alongside Overall. They show at most three server-authored observations:
the requested focus (scoring for general overviews), a supported comparison or
game-level variation when available, and relevant production beyond scoring or
playing-time context. General change notes read assists with turnovers when
those changes are supported. Defense and rebounding remain separate observations.

Incomplete or thin core focus samples produce one sample warning, without filler
or a standout claim. Comparison notes use only `display_change` values that pass
the existing sample rules and the overview's 0.1/2%-of-current-value descriptive
change threshold. Complete scoring samples can show a game range when its spread
is at least five points or 30% of the period average (whichever is greater); this
is a display rule, not a significance or consistency test. Minutes accompany
production changes when comparable. Qualified ratio ranks do not select takeaways, and no
highest-percentile or composite-impact selector is introduced.

Saved results retain their own server-authored takeaways, with at most three
visible rows. Missing canonical metadata receives the saved-answer notice.

Older saved results preserve their original metrics and receive a saved-answer
notice when presentation metadata is absent. Ask again to apply the new policy.


## Compact labels and changes

Table rows, sample rows and chart controls use metric abbreviations. Full names
remain in evidence/prose and accessible row labels. TS% has an on-demand tooltip
available by hover, keyboard focus or tap; Escape, a second tap or moving focus
closes it. It shows `100 × SUM(PTS) / [2 × (SUM(FGA) + 0.44 × SUM(FTA))]`, using
summed components from valid appearances rather than averaging game percentages.

Change colors use the visible signed delta: green for increases, red for decreases,
with TOV reversed. MIN, values rounding to zero and unavailable comparisons remain
neutral. Signs and units remain visible without relying on color. Colors describe
the metric direction, not an individual impact or defensive grade. Existing sample
and baseline rules continue to withhold unsupported comparisons.

## Performance over time

The server emits `scope.chart_grain`, chart `grain` and each basic metric's
`trend: {grain, points}`. Inclusive windows of at most 31 days use `daily` game-date
buckets; longer windows use `monthly` averages. Monthly results also retain the
legacy `monthly` points. Saved charts without grain metadata remain monthly.

Named months use an explicit or disclosed inferred year; their comparison is
the preceding full calendar month. A `week of/starting/beginning YYYY-MM-DD` is
seven inclusive days starting on that date. This/last week use Monday boundaries;
this month runs through the anchor date and compares with the preceding full
calendar month; last month is the preceding full month.
Past week/month are rolling windows; numeric rolling windows retain their rules.
Relative calendar windows honor a supplied as-of date. Partial
month/week phrasing and competing windows require clarification. Future ranges
remain invalid. No date request falls back to another season.

The UI title is **Performance over time**, with **Game-by-game** or **Monthly
average** below it. Points use only recorded game dates or months with appearances;
no off-day zeroes are inserted. Missing metric components still withhold the trend.
Date/sample tooltips preserve full dates, while axis labels are thinned to avoid
crowding. Phone overview charts fit their container and hide dense value labels;
each value remains available on hover or focus. The eleven-row, four-column table and collapsed context remain unchanged.


## Follow-up panel

The continuation composer uses a subtly tinted surface, orange left edge,
regular Barlow heading **Ask your next question**, and **Continue the conversation**
eyebrow. Player names and governed date/phase scope remain in the context line.
Suggested questions still fill the composer without submitting; Enter submits,
Shift+Enter inserts a newline, and scope changes/restored chats retain the existing
server/browser context behavior. The textarea starts at two rows/60px, with manual
vertical resizing available. This styling also applies to comparison follow-ups.


## Metric distributions

Overview ranked metrics carry `ask_metric_distribution/1` evidence. Among players
uses exactly the same qualified cohort as the percentile, with pooled percentages
or per-game averages. Peer histogram counts must sum to cohort size. A finite
unqualified focal value is shown as a dashed marker outside the cohort, without
assigning a percentile. Median, quartiles and extrema use linear interpolation at
`(n - 1) * p`; quartiles describe observed spread, not confidence intervals.

Across games uses individual valid recorded appearances in the same scope.
Missing components and zero denominators are omitted and counted separately;
recorded zero-event counts remain observations. Shooting game medians are distinct
from pooled period percentages; per-game attempts accompany the observations.
TS% observations expose both FGA and FTA. A compact game selector supports touch
and keyboard inspection alongside the chart dots.
Dots are bounded to the latest 300 valid appearances, with truncation disclosed;
summary statistics and histograms always retain all valid observations. Integer
count distributions retain discrete bins when the range has at most 41 values;
otherwise histograms use 12 equal-width bins. Membership follows the emitted
left-inclusive boundaries; the final bin includes its maximum.
Constant samples use one bin and empty distributions remain empty.

The percentile cell opens a native modal metric drawer without expanding table
rows. It is a 440px side panel on desktop and a bottom sheet capped at 78dvh/640px
on phones. The initial view is Among players; Across games is a second toggle.
Escape/close return focus to the originating metric. The TS% calculation tooltip
remains separate. Saved responses without distribution evidence retain their
existing controls and values; the browser never reconstructs distributions.
