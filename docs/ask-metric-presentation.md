# Ask overview metrics and samples

The server owns `semantic_evidence.presentation`, version
`ask_metric_presentation/1`, including selected metric keys, sample notes,
question focus, limitations and ordered takeaways. The browser renders those
facts without selecting the largest percentile as the headline.

## Visible metrics

| Focus | Primary metrics |
| --- | --- |
| General | PTS, TS%, 3PM, 3P%, AST, TOV, REB, STL, BLK, MIN |
| Scoring | PTS, TS%, 3PM, 3P%, FGA, 3PA, MIN |
| Playmaking | AST, TOV, PTS, TS%, REB, MIN |
| Rebounding | REB, MIN, AST, TOV, STL, BLK |
| Defensive activity | STL, BLK, MIN, REB, AST, TOV |
| Contribution beyond scoring | AST, TOV, REB, STL, BLK, MIN |

General overviews show ten metrics. Focused broad performance/profile questions
show six or seven; remaining calculated metrics are expandable. The main table uses four compact
columns with inline %/minutes and percentage-point changes. Sample details and
contribution explanations use keyboard-accessible disclosures; limited-data
labels remain visible beside affected values. Explicit metric,
venue, opponent, ranking and unsupported grain requests retain their existing
routes and scope guards. A focused overview is a presentation of whole-game
observations, not permission to substitute them for unavailable tracking data.
Current overview scopes support season and calendar windows; last-N overview
requests remain unsupported and never become a calendar window silently.

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
Shooting percentiles are never inferred from a games-only floor. Ratio samples
remain explicitly descriptive because no reviewed attempt threshold or precision
rule has been established. Rich defended-shot/chance rules must be defined and
validated with those sources before they can be implemented.

## Takeaways

General contributions use fixed scoring, creation, rebounding and defensive-
activity order. Focused questions lead with their requested area. Assists appear
with turnover and playing-time context; total rebounds disclose missing splits;
steals/blocks disclose the limit on overall defensive interpretation. Limited
metrics cannot create a standout claim. The server's answer paragraphs and
ordered takeaways use unrounded validated evidence. No composite impact score,
causal interpretation or best-looking percentile selector is introduced.

Older saved results preserve their original metrics and receive a saved-answer
notice when presentation metadata is absent. Ask again to apply the new policy.
