# Overview insights and private monitoring

Broad Overview responses explain supported relationships instead of listing all
metrics. The deterministic selector considers scoring, creation, rebounding and
steal/block activity, returning at most three findings. Each finding exposes an
ID and supporting metric keys in `semantic_evidence.insights`; API prose and
takeaway titles share those findings. Saved answers are not rewritten.

## Evidence and selection

- Personal comparisons use eligible full-precision changes. Per-36 rates use
  aggregate production and minutes, never averages of rounded game rates.
- More points plus more attempts and non-increasing true shooting supports a
  volume/efficiency contrast. Per-36 changes distinguish exposure from scoring
  rate. This is arithmetic description, not an injury, opponent or lineup cause.
- Assists are interpreted alongside turnovers. Their peer standing and personal
  trend are separate facts and may point in different directions.
- Peer prose requires `ask_overview_percentiles/1`, ranked status, complete
  components, focal inclusion and a matching distribution cohort of at least two.
  The text names the phase, cohort count and minimum appearances. It compares
  all qualifying players, not position or role-matched peers. Strictly worse
  peers define the percentage; tied players are not counted as worse.
- Additional activity is selected for a change of at least 0.5 and 10% of the
  current value, or an upper-quartile peer result (lower-quartile rebounding also
  qualifies). These are editorial thresholds, not significance tests. Rare-event
  counts never become a defensive impact grade.
- Missing and partial inputs are omitted from broad prose, with metric sample
  details retained in tables. Complete thin samples keep values and a short
  sample note; comparisons remain withheld. Direct requests for unsupported
  measures still receive a truthful refusal. Generic capability lists are omitted.

## Concise editorial rendering

Renderer `ask_overview_insights/4` leads with the player finding. Broad responses
have one or two narrative paragraphs, an optional game-variation paragraph,
and a compact scope paragraph.
`summary_context` identifies that final paragraph for subdued UI styling; it
remains part of `summary_paragraphs` and the identical JSON/SSE answer text.
The scope line records current appearances, baseline appearances and dates when
comparison is eligible, and one qualification description per distinct peer
cohort used in prose. Missing baselines never imply usual performance.

Sidebar bodies summarize the supported relationship and decisive evidence,
rather than copying the first sentence. Optional activity joins the final
narrative paragraph. A substantial change or notable level may qualify on its
own, retaining stable elite performance. Material activity changes take priority,
then rebounding, steals and blocks resolve ties explicitly. Low steals/blocks
are not interpreted as adverse defensive grades.

True-shooting movements below 0.5 percentage points use “similar”; movements from
0.5 up to 1 point use “slightly” higher/lower. These are editorial thresholds, not
significance tests. Both directions of disagreement between assists per game and
per 36 are surfaced. “Led” requires an exact unrounded strict-worse percentile of
100; near-100 values never imply a unique leader. Rank qualification and ties
remain governed by the existing metric policy.

## Backend events

The existing private service logger emits `overview_capability_tracking` records
after requests, separately from responses, SSE events and browser history:

| Kind | Meaning |
| --- | --- |
| `overview_assessed` | Denominator for evaluated overview requests |
| `explicit_demand` | A request mentions an allowlisted capability |
| `evidence_gap` | A considered metric lacks eligible data, baseline or peers |
| `product_feedback` | A recognized explicit complaint about overview quality |

Capability requests and product-feedback detection use bounded phrase rules.
These are observed signals, not a classifier of all user intent. Negated requests
are excluded where recognized; repeated mentions within a request are deduplicated.
Gaps do not imply demand. No event is generated for every imaginable missing
measure: only considered evidence or a user's actual mention is recorded.

Fields include request ID, allowlisted capability/reason, renderer version and
outcome. Overview-derived events also include player ID, date/phase scope and the
snapshot reference. No question, answer, credential or conversation ID is logged.
Events have a per-request bound and use the existing best-effort logging path;
sink failures must not change answer delivery. They are not an exactly-once
durable queue. No public inbox or feedback-reading endpoint is added.

Export only these structured records to access-controlled operational storage.
Use the deployment's restricted log access; do not publish the raw export or add
it to Git. Keep local exports under ignored `reports/` with owner-only permissions.
The code does not provision cloud logging permissions or retention: before a
production rollout, configure the destination with a 30-day raw-event retention
and delete local raw exports after review (within 30 days). Aggregate counts can
be retained separately without request IDs. Existing privacy boundaries apply.

Summarize an extracted private JSONL export with:

```sh
python scripts/summarize_overview_tracking.py reports/private-overview-events.jsonl
```

The command deduplicates request/kind/capability/reason, emits separate counts and
an overview denominator, and omits request IDs from its output. Count unique
requests for each gap rather than summing reasons to calculate an affected-request
rate. Explicit demand includes non-overview requests, so do not divide it by the
overview-only denominator.

## Verification

Run the insight, tracking, summary, performance and endpoint tests; then the full
Python, JavaScript, semantic and browser suites. Use the same cases, input source
and grader for before/after endpoint runs. Verify peer counts and statistic totals
independently for a captured warehouse example. Retain failed attempts privately.
Fixture, captured-warehouse endpoint, browser and deployed validation are distinct.

## Position comparisons and focused profiles (v3)

All six Overview profiles use shared structured insights. Focused profiles keep
only relevant findings; scoring and assist summaries preserve material opposing
per-game/per-36 directions. Two-appearance answers have a concise narrative and
no repeated takeaway cards. Explicit position comparison clauses resolve their
baseline relative to the named season before generic relative-time defaults.
First-two-appearance windows resolve observed dates in the named season (regular
season by default); peers use those same dates, not each peer's first two games.

Existing metric `percentile` and `distribution` remain league-wide. Counting
metrics PTS/AST/REB/3PM/STL/BLK additionally carry `peer_comparison` under
`ask_position_peers/2`. It includes the selected reference, dates/phase, season,
qualification, sample size, competition rank/ties, strictly-worse percentile,
coverage, availability reason and its own matching distribution. The frontend
labels the table League percentile and exposes separate Overview peer evidence.
Shooting ratios retain existing pooled-attempt eligibility and league context;
minutes and turnovers do not become praise-oriented position headlines.

Position mapping `position_groups/2` uses season-specific listed positions:
G/PG/SG → Guard, F/SF/PF → Wing/forward, C → Big/center. G-F/F-G belongs
to Guard and Wing; F-C/C-F belongs to Wing and Big. Hybrid membership overlaps,
but a player counts once in each cohort. These are roster categories, not
measured on-court roles. Label the references **wings/forwards** and
**bigs/centers** so readers can distinguish the grouping convention. Never infer
primary position from label order, height or ranked statistics. An implicit
request for a hybrid player has no invented primary group. An explicit group
uses that group if it is one of the player's documented memberships.

Configure `AGENT_POSITION_CONTEXT_PATH` to a private JSON bundle (up to 1 MB).
`historical_positions.roster_records` validates a complete 30-team capture of
NBA `CommonTeamRoster`: request season/team/league, row season/team/league,
exact player IDs, supported positions, row widths and duplicate identities.
An archive must retain the original NBA response, source URL, retrieval time
and SHA-256 of the retained bytes. Do not use today's profile for a past season.
Final rosters can omit waived players; review coverage against **every** eligible
warehouse player and retain the missing-player audit. Fill gaps only from dated
team roster tables or announcements explicitly naming the position. Verify
curated IDs/names/teams against the scoped warehouse; keep exact source page or
transaction locators. Search extracts must be marked as such in the private
audit, not described as original publisher bytes. A checksum proves replay
integrity, not publisher authenticity or completeness.

Build a new bundle with:

```sh
python scripts/build_position_context.py \
  --manifest reports/position-review/manifest.json \
  --output reports/position-review/position-context.json
```

The private manifest contains `archives` entries with `season`, `path`, `url`,
`sha256`, a `reviewed_seasons` list matching the archives, and optional
`supplements` with `path` and `sha256`. Paths resolve beside the manifest.
Supplement records contain `season`, `player_id`, `position`,
`provenance: dated_team_document`, HTTPS `source_ref`, `source_sha256`,
`source_date`, and `source_locator`. The operator must inspect those retained
sources before approval. The builder validates hashes and duplicate identities,
and writes create-only; failures cannot overwrite an approved bundle.

The resulting bundle contains the mapping version, approved seasons, normalized
records and its own canonical SHA-256. It is also included in the statistical
snapshot digest. Legacy mappings and tampered bundles fail closed. Missing or
invalid optional evidence cannot enable a position comparison. An unreadable
configured file remains an explicit source error. Do not commit raw captures,
curated player records, audits or local deployment configuration.

At runtime at least 95% of the metric-eligible player cohort must be classified,
the focal player's verified group must match an explicit requested group, and
there must be at least 10 eligible group members. A 10–19 member reference uses
exact ordinal standing; 20+ permits percentile prose. Smaller references and
all-equal values do not support percentile interpretation. These provisional
presentation gates do not establish statistical precision. League references for implicit requests
use the same prose gates; legacy table ranks are unchanged. Without an explicit
group request, a unique league leader may retain league-wide scope under a fixed
rule. Selection never maximizes favorable percentiles. No blanket minutes floor
is imposed on per-game metrics.

Explicit group requests never substitute the league cohort. Missing membership,
coverage, or sample eligibility produces a withheld comparison, a short direct
statement that the requested comparison is unavailable, and
`request_fulfillment.status: partial`. This is distinct from an answered
comparison; HTTP success alone is not fulfillment. Generic missing-capability
paragraphs are not added. Private
`comparison_opportunity` events record bounded reasons, versions, reference and
size/coverage buckets, separate from `explicit_demand`. These new events omit
player identities, raw questions, answers, conversations and rows. Existing log
access/retention requirements still apply. Current warehouse position availability
must be audited independently; code support does not assert data availability.


## Question-first comparisons and variation (v4)

An explicit Guard/Wing/Big request controls the first finding and sidebar. With
verified membership, the answer gives ordinal standing, season/phase, the gap
from the group median, and per-36 context over exactly the same cohort when all
members have valid minutes. Group medians include the focal player; rank and
percentile tie semantics are unchanged. Personal baseline changes appear only
when requested alongside an explicit group comparison. Legacy league table
statistics remain clearly labeled and never become a substituted Overview answer.

The selected metric follows the profile: points for general/scoring, assists for
playmaking/contribution, rebounds for rebounding, and steals for defensive
activity. At least five complete games are required for representative spread
prose. The existing linear `(n-1)` quartiles supply the middle 50% and median;
these describe games, separately from the distribution of player season means.
No comparative consistency or causal claim is inferred from those quartiles.

Counting-metric highs are calculated over every scoped row before the 300-dot
chart cap. The earliest date then game ID breaks display ties; the text gives
the tie count. Dates are validated and opponents are included only when recorded.
Equal-valued seasons have no arbitrary standout sentence. Shooting percentages
are not given highest-game praise based on small denominators. Missing values
and tiny samples withhold representative-spread prose, and two-game answers
retain their sample label without a fake percentile or consistency conclusion.

The compact expandable game chart labels the metric, dates, median, middle-half
band, and highs. Focused sidebars add variation and dated-high cards. Structured
`game_variation` evidence links prose to the same metric distribution. Private
tracking records bounded variation/standout demand and requested-comparison
fulfillment; raw questions, answers and backend events are not added to the UI.
