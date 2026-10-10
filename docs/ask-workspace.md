# Ask workspace

Every tab opens on the latest supported season (currently 2025–26). There is no
shared season selector; old page links with a season parameter redirect to the
latest view. Explicit historical API requests remain supported.

Ask accepts historical seasons in the question, including “last season.” Without
a stated period it starts with the latest season and checks supported archives
newest first when season evidence is unavailable. Available teammate studies use
their published date window. Answers disclose an earlier-season fallback and its
scope. Explicit seasons/dates and conversational follow-up scope are preserved;
permission errors and invalid evidence never trigger fallback. This does not
create missing studies or infer an injury absence from a missing appearance.

Named absence questions check the published pair's coverage before warehouse or
model work. A missing comparison names the pair and explains that its verified
study must be built or reconnected; an overall-player question is offered only
as a separate follow-up. Broken catalog configuration, invalid planner output,
and unavailable research evidence have distinct safe error codes. Paths and raw
provider/data errors are not exposed. Published studies still require exact
season, date, phase, role, and filter matches.

Complete, unqualified questions such as “Tell me how LeBron James played while
Luka was out” use a deterministic study plan for a registered pair. They read the
published catalog directly, without warehouse access or model planning, and use
the same season, phase, date and evidence guards as model-planned requests.
Added filters, metrics or dates and conversational follow-ups stay on the scoped
planner path. A partial catalog replaces a legacy study only when it publishes
that same pair; invalid catalogs still fail closed. Local operators must build,
validate and connect `RESEARCH_STUDIES_PATH`; code deployment alone does not
publish private study evidence.

The research planner's structured schema binds metrics to its selected route.
Studies support the nine core box-score measures and eight shooting measures;
per-36 requests remain supported by breakdowns only. Explicit unsupported study
metrics are not silently dropped or replaced. The server validates the plan
before rendering, even when a provider returns output outside the schema.

## Two-player comparisons

Explicit `A vs B`, `A versus B`, and `Compare A and B` questions use a
deterministic two-player scorecard. Nicknames use the existing source-backed
alias catalog. Each side is resolved separately; ambiguous identities require
selection and already-resolved sides are retained during clarification.
The scorecard shares one date/phase scope and one qualified cohort per metric
across points, rebounds, assists, steals and blocks. Minutes and sample sizes
come from the same appearances. Differences use unrounded averages; missing
components withhold edges and percentiles rather than becoming zero.

The established winning-impact section currently has **unavailable values**.
The governed player-game source has no Win Shares or on/off possession totals.
No external metric feed has been added, no custom contribution score calculated,
and raw plus/minus is not substituted for possession-based net rating. The net
rating chart selector explains this missing-data state; the five core chart
selectors remain usable. Completing advanced analytics requires a separately
verified matching-scope source and its integration, not a UI placeholder change.

Supported scope is a shared date range and regular season, playoffs, or both.
Explicit playoff years resolve to the corresponding season. Round, clutch,
opponent, home/away, wins-only and per-possession/per-minute filters are not
silently approximated. Period-versus-period questions keep their existing route.
Saved comparison context includes both full names and explicit dates. Fill-only
follow-ups preserve that scope, and an assists follow-up selects the AST chart.
Narrow-screen tables scroll horizontally and are keyboard-focusable.

Ask uses one tab per conversation. Five tabs are visible on desktop, including
the new-question draft. At smaller widths, fewer tabs are shown; More remains
available. Opening a sixth chat moves the oldest open tab out of the strip. It
does not delete the conversation. Closing a tab has the same non-destructive
storage behavior. Follow-ups remain in their original conversation.

More opens searchable local history. Search includes opening questions and
follow-ups, and the local endpoint pages beyond the browser's recent cache.
Opening a result restores the stored response payload without rerunning queries.
All questions and responses remain visible in chronological order, with each
response owning its profile, methodology, tables, and interactive charts. The
question selector jumps to an existing response without replacing content.
Suggestions fill the bottom follow-up composer without submitting.

Each tab, including an empty draft, receives its own conversation UUID at creation.
Drafts, pending requests, errors, and answers belong to that conversation. Switching
tabs does not retarget an in-flight request; streaming and JSON responses are checked
against the originating conversation and request IDs. A reopened saved chat retains
its ID, while New chat always creates a new one. These IDs are navigation keys, not
authentication credentials.

## Follow-up context

The last successful analysis supplies a bounded structured context: resolved
identities, date/phase scope, metrics, and a short answer summary. Follow-ups
receive that context without requiring pronoun keywords. Explicit new dates,
phases, or full player names override inherited intent. Clarifications do not
replace the last successful analysis. Prior prose is context, not fresh evidence.

Availability answers retain both focal and teammate identities in this shared
context, along with the separate availability predicate. Saved history and bounded
browser recovery preserve the pair and validate legacy IDs against the source.
References such as “each player” and “both players” resolve within this conversation.
A season games-played follow-up counts each player's recorded appearances from the
full player-game evidence, not the previous comparison samples. It carries player IDs,
scope, snapshot provenance and game IDs in `appearance_evidence`; unsupported extra
conditions are withheld. Availability-specific follow-ups retain their original
predicate, while a successful season-count answer becomes an ordinary appearance
analysis. Explicit new player names and periods override the prior request.

For an overview follow-up such as “besides Johnson, who are the other top
playmaking leads?”, a unique contextual surname resolves to the prior player;
the ranking excludes that identity and keeps the exact dates, including across
supported seasons. Assists per game is the disclosed playmaking interpretation
when the prior overview included assists and no alternative measure was given.
Exclusions hide the named player before applying the result limit; ranks and
percentiles still describe the full qualified league cohort.

## Persistence and boundaries

- Browser cache: the existing season-specific `askChatHistory:v1` key remains
  compatible. Up to 25 recent conversations (favoring open tabs) are cached;
  storage failures are surfaced rather than silently discarding in-memory work.
- Navigation/drafts: `askOpenTabs:v1:<season>` stores tab order, the active tab,
  and per-conversation drafts. Closing a tab retains its draft.
- Local history: when `AGENT_HISTORY_ENABLED=true`, the existing JSONL file
  retains all recorded turns. `/api/agent/history` supports `q`, `offset`, `limit`
  and exact `conversation_id` filtering. Local-access checks are unchanged.
- A restarted server can rehydrate conversational context from that local file.
  Governed overview context uses the saved explicit dates, not a newly evaluated
  relative window. Browser-only chats send bounded hints from the latest
  successful response when server context is missing: question, player identity,
  explicit dates, phases, and metric names, never cached statistics. Player IDs
  and names are checked against the loaded source. Existing server context wins;
  failed turns and review navigation do not replace the successful context.
- This is local persistence, not authenticated multi-user or cloud-synced history.
  Tabs share browser-origin storage; simultaneous independent browser windows
  are not a collaborative editing surface.
- Navigation within Ask is disabled during a request
  so streaming responses cannot land in another chat. Partial-stream failures
  are shown without automatically issuing a second paid request.

## Analysis presentation

Single-player research breakdowns and matching teammate-study answers include
the focal player's name, headshot, profile link, and the answer's season, phase,
and dates. Breakdown cards use team and appearance counts from the filtered
games. Frozen studies omit those fields when scoped appearances are unavailable;
they never borrow current rankings or team context. Multi-player breakdowns and
scope refusals do not select an arbitrary player for a card.

Teammate comparisons lead with a basketball takeaway, a few observed differences,
and the counts of games when both played and when the teammate was out, stated
once when those counts match across the available metrics. A short natural caveat
provides context for thin samples. Season and dates remain visible; technical
assessment labels and raw study diagnostics stay in server evidence and logs.
Saved responses retain their original text; new requests use this presentation.

Governed player overviews use the same evidence for the overall assessment,
up to three supported takeaways, five-stat percentile table and monthly chart.
The chart initially selects the strongest eligible category; PTS/REB/AST/STL/BLK
controls switch the series. Other Ask intents retain their original answer and
table/chart renderers instead of receiving invented overview insights.

Supported broad overviews use three ordinary paragraphs: scoped scoring,
playmaking and efficiency; a named comparison; then game-level variation.
Stable scoring remains relevant and small rebound changes do not become the
headline. Complete comparable data is required for changes, which use a display
threshold of at least 0.1 and 2% of the current value. These are descriptive
observations, not causal claims. The backend and browser share the same prose.
Older saved responses retain a short fallback until the question is asked again.

Full playoff overviews compare with the same season's regular season. Season
overviews compare with the corresponding prior-season dates; calendar windows
compare with the preceding window (calendar months for month requests).
Explicit split questions retain their user-selected groups. Both current and
comparison phases are recorded and displayed; missing baseline coverage
withholds changes. Finals, championship series and individual playoff rounds
are unsupported until verified round-level data is connected. They never fall
back to full-season or all-playoff statistics. Finals-to-earlier-round and
Finals-to-regular-season comparisons therefore remain unavailable.

Game insights use the same scoped player appearances: scoring minimum, maximum,
median, 20-point game count and a dated high (ties disclosed). Any missing scoring
value withholds the distribution. Samples below five games carry a caution.
Narrow metric requests, awards and unsupported requests remain concise. No-data
responses do not pad out three paragraphs. All detailed metrics remain in the
tables and evidence, with their original units.

The overview describes relative category strength, monthly variation, and
box-score activity, with additional minutes, attempt, shooting-efficiency,
turnover, and per-36 context. These metrics do not establish causal drivers.
Opponent and selected teammate summaries are available in the offline evaluation;
see [Player context](player-context.md) for integration and coverage limits.
Thin percentile bars use native meters. Icons are vendored Tabler assets under
their MIT license; the existing locally hosted Barlow fonts and real player
headshots are reused.

See [Players and research breakdowns](research-workbench.md) for shared detailed queries, multi-stat teammate studies, and versioned pregame context.

Teammate comparisons default to regular season plus playoffs for the selected
season. A December-ending pilot is not a full-season fallback. Published studies
must bind their phase and window; explicit dates or phases cannot reuse a
differently scoped result. See [study scope](research-workbench.md) for full-season
build checks and missing-status handling.

### Visualization specialist

After the answer is grounded, `VisualizationAgent` selects a chart from bounded,
server-built candidates. Teammate contrasts use one metric and two observed-group
bars; governed rankings use bars; game logs use chronological lines. When several
verified metrics are available, a separate model call selects only a candidate
ID using the question and answer. It cannot supply values, code, SQL, or new data.
A single candidate needs no model call. Selection failure logs a safe error type
and uses the first evidence-ranked candidate without failing the answer.

Charts include their units, scope, a short description, and an explanation of the
chart choice. Hover, tap, and keyboard focus expose value/sample details. Bar scales
include zero and preserve negative values; missing values are never zero-filled.
Long category labels wrap without truncating player names or availability status,
and chart rows grow to keep labels separate at desktop and narrow widths.
Unsupported answers and evidence without a suitable chart remain text/table-only.
Existing specialized player overview/comparison charts retain their own rendering.

The collapsed header panel **Source & Coverage** combines publication status and
asset coverage with the selected answer's scope, assumptions, and metric
definitions. Selecting a saved question updates those details; starting a new
chat clears them. Answers no longer repeat a Methodology & data disclosure.
Material interpretation caveats remain in the answer and chart descriptions.

Teammate answers include group averages, absolute differences, and relative changes
using unrounded averages with the both-played group as baseline. Relative changes
are omitted for zero/negative baselines, missing components, shooting percentages
(which use percentage points), and plus-minus. The expandable **Explore the
difference** view compares minutes and per-36 production, then plots one dot per
classified appearance. Per-36 rates use 36 times total production divided by total
minutes and require complete records within each group; they do not adjust for
opponents or role. Missing game values are omitted, never zero-filled. Hover, tap,
or keyboard focus exposes date, opponent, phase, value, and game ID. Only public
box-score fields are projected from the private study panel.

### OpenRouter

Model settings includes OpenRouter with `deepseek/deepseek-v4.1-flash` and
`qwen/qwen3-235b-a22b-2507`. Set `OPENROUTER_API_KEY` in the server environment
or local `.env`, then restart the server. Optional `OPENROUTER_AGENT_MODEL`
selects the default (DeepSeek); `OPENROUTER_AGENT_TIMEOUT_SECONDS` defaults to 90.
The provider is visible before a key is configured; selecting it without a key
returns a configuration error and never uses another provider's credentials.

The adapter uses OpenRouter Chat Completions, translates the existing allowlisted
function tools and JSON schemas, and requires endpoints that support the requested
parameters. Model IDs are fixed; OpenRouter may route among providers for that
same model. Tool-call IDs and reasoning details are preserved for continuation.
Existing scope/evidence validation remains authoritative. Shared Ask retry limits
apply, with SDK retries disabled. SSE progress remains available; token-by-token
answer streaming is not enabled for this adapter. Mock checks do not establish
live endpoint availability, schema compatibility, latency, or cost.

## Dynamic teammate availability

With `RESEARCH_AVAILABILITY_PATH` connected, Ask resolves player roles from the
source-backed directory and computes descriptive splits on demand. Any observed
pair can be queried; neither registration in the pilot catalog nor a pair-specific
artifact is required. Full names and known aliases are supported; an ambiguous
first name can be narrowed by a uniquely matching shared team in the requested
season, otherwise Ask requests full names. Reversed roles produce a different
sample rather than reusing the original pair's result.

Supported requests compose box-score metrics, shooting ratios, points/assists per
36, averages/totals, one season, explicit inclusive `from YYYY-MM-DD to YYYY-MM-DD`
dates, regular season/playoffs/both, home/away, and opponent abbreviation. Scoped
follow-ups such as “What about assists?” or “Only the playoffs” recalculate with
the saved player roles and remaining filters. The parser accounts for the entire
request; unsupported phrases, ambiguous roles and conflicting filters produce an
explanation, never a partial season summary. Ordinary semantic planning/answering
also blocks availability conditions, so a model cannot discard them. The old
published-study route remains compatibility support when dynamic evidence is not
configured; it is not a prerequisite when dynamic evidence is available.

Availability policy `availability/2` classifies final participation before sample
eligibility. Positive final minutes mean **played**, even when a pregame report
said Out; the report disagreement remains an audit warning. **Did not play**
requires a same-team zero-minute/final DNP record or a valid game-specific Out
report and no appearance. A missing row alone never establishes non-participation
or team membership. Conflicting membership, unknown minutes, stale/late reports,
and unfinished games remain excluded. Neither absence nor low minutes establishes
injury causation. The prior `reported_out` evidence group becomes `did_not_play`;
old saved answers are explicitly marked as using earlier rules, not recalculated.

Each classification records its basis: positive/zero-minute statistics identify
the statistics snapshot and season/game/player key; final box scores and Out
reports retain their respective supporting URLs. A statistics-based DNP does not
cite an unrelated injury bulletin as its classification evidence.

Default comparisons label the phases actually covered by the source and disclose
any missing phase in the answer and chart. An explicitly requested uncovered
phase is refused. Follow-up context uses the covered phases, and games-played
follow-ups retain the prior season unless the new question explicitly changes it.
Displayed end dates do not extend beyond the observed source records.

Informational phrases such as “find out” do not trigger availability analysis.
Minimum-games rankings remain on the governed statistics route; explicit
appearance-count questions use the count route. Existing research follow-ups
retain their research scope.

Empty chat drafts live in tab state and do not consume the saved-history limit.
Returning from history restores the active chat, including answers completed
while the history panel was open, and synchronizes the submission controls.

By default, exclude an appearance when the focal player, or the teammate when
both played, has minutes **strictly below 50%** of their own median over the
previous ten positive-minute final appearances that season. At least five prior
appearances are required; insufficient baselines stay included and are marked.
Baseline games precede the evaluated game and ignore comparison date, venue,
opponent and phase filters. Limited-minute appearances still count in ordinary
games-played totals and are never reassigned to the absence group.

Follow-ups **Include limited-minute appearances**, **Exclude limited-minute
appearances**, and **Use a 60% minutes threshold** change this policy within the
current tab. Thresholds must be above 0% and at most 100%. Options persist through
follow-ups and restored context. Each answer reconciles all scoped records into
both included groups and mutually exclusive exclusions. Expand **Game inclusion
details** for dates, opponents, participation, reasons, actual minutes, prior-game
baselines and data warnings. Evidence also preserves baseline game IDs. Empty
comparison groups retain that audit and unavailable values; no substitute sample
or comparison chart is returned.

Missing metric components retain valid/observed denominators and suppress
incomplete differences. Percentages use pooled components; per-36 uses pooled
production and minutes. Statistics, labels and the scoped player card use only
the included sample, with the full scoped record count disclosed separately.

`availability_evidence` accompanies each response with the exact executed request,
per-metric component totals/denominators/game IDs, group membership, report URLs,
exclusion counts, and immutable evidence and stats hashes. Player card, narrative,
table and chart all use those same groups. Responses are deterministic, make no
provider calls, and do not expose model-authored SQL. This capability does not
support arbitrary natural-language operations, causal claims, unverified injury
causes, or undocumented constraints; those are withheld explicitly.

Build an immutable league-wide source bundle, then connect its path locally:

```sh
python scripts/build_availability_evidence.py \
  --snapshot reports/inputs/stats.json \
  --schedule reports/inputs/schedule.json \
  --injuries reports/inputs/injuries.json \
  --output reports/availability/unique-run.json
```

The snapshot must have the semantic source checksum and coverage contract; the
schedule is the official schedule document and injuries contains `rows` with the
existing pregame report provenance fields. The builder validates source joins
before create-only publication. The reader verifies both bundle and stats hashes,
unique keys and schedule alignment. Reads are size-bounded and cached by immutable
file identity; source artifacts and local configuration stay out of Git. Updating
underlying data requires publishing a new validated bundle and connecting it;
merging application code does not automatically refresh evidence.

### Metric naming and sample counts

Use the shared presentation helpers in `app/agent/metric_presentation.py` for new
metrics and renderers. Availability tables label sample completeness **Games with
data — both played** and **Games with data — teammate did not play**, formatted
as **39 of 39**. Single-group tables use **Games in scope** and **Games with data**.
The table description explains the numerator and denominator. These labels mean
complete required inputs for that stat; they do not imply an entire season, an
absence caused by injury, or a calculable rate when attempts/minutes are zero.
Saved availability tables receive the same presentation without rewriting evidence.

When adding a metric, provide a readable name (familiar basketball abbreviations
such as PTS are acceptable), its unit and aggregation, explicit comparison groups
and difference direction, and a definition of any sample count. Avoid internal
terms such as "valid/observed" in user-facing labels. Keep completeness per metric;
never substitute the group size for games with complete inputs. Render missing
values as unavailable, retaining zero as zero. Include a partial-data case in the
metric's validation and check the populated table and chart at a narrow viewport.
Presentation helpers apply to all selected metrics, so new metrics inherit these
coverage labels without player-specific or metric-specific copy.

Repair older report bundles with `scripts/repair_availability_evidence.py`. It
reparses official PDFs referenced by inconsistent team/matchup rows, retains raw
sources and full repair audits, and publishes a new immutable bundle only after
validation. Optional final box-score capture adds explicit participation records;
`--reports-only` performs the report repair without claiming that capture.
`--cached-reports` reuses previously downloaded PDFs. The source builder/loader
rejects inconsistent report team/matchup records. Connect the new bundle explicitly;
never overwrite the prior snapshot or silently change historical answers.

## Descriptive player splits

Ask recognizes a comparison's axis before resolving comparison sides as players.
Singular “playoff” and “postseason” are phases. A deterministic player-split route
supports one observed player and one season, with no model calls:

- Regular season versus playoffs.
- Home versus away, optionally restricted to one phase or both.
- Last N versus prior N appearances, with equal N from 1–100 and an optional
  as-of date. The two windows are disjoint.
- Before and after an explicit ISO event date. “After” includes that date;
  “before” ends the preceding day. A trade/injury date is never guessed.

Examples: “Compare Avery Example FG% home vs away in 2024-25”, “Avery Example
regular season vs playoff in 2024-25”, and “Compare Avery Example points before
and after 2025-01-01 in 2024-25”. The name must resolve in the selected source;
the example identity is fictional. Shared date ranges and an opponent abbreviation
compose with phase, venue and event comparisons. Explicit historical dates are
never replaced with current-season data. Unknown conditions withhold the answer.

Unspecified metrics return points, rebounds, assists, minutes and TS%. Ratios use
pooled components; differences use unrounded values and are withheld if either
sample is incomplete. Evidence includes both executed queries, metric components,
game IDs, coverage and source provenance. Tables label comparison direction and
per-game/total units. A metric-only follow-up keeps the player and frozen split
scope, including after browser-context recovery. Every follow-up recalculates.

“Why” or “better” within a supported split receives an explicitly descriptive
partial answer, not a causal conclusion or overall ranking. Possession/lineup
requests identify the missing data grain. Fantasy decision questions request
league scoring and roster context instead of treating built-in proxy scores as
league rules. These guards do not provide forecasts or support every natural
language phrasing. Other research and availability contracts remain distinct.

## Award lookup and performance composition

Award questions use one shared resolver backed by
[`award_catalog.json`](../app/agent/award_catalog.json). The catalog defines award
names, aliases, default performance scope and source-backed winner records.
Adding a reviewed award or season requires catalog data, not a handler branch.
The seven connected awards are MVP, Rookie of the Year, Defensive Player of the
Year, Sixth Man of the Year, Most Improved Player, Clutch Player of the Year and
NBA Finals MVP. Each covers 2023–24 through 2025–26; official NBA winner lists
were checked on 2026-10-05. Each record retains its own URL and verification date.

The resolver recognizes long names and common abbreviations before player
resolution. Longest aliases take precedence, so Finals MVP is distinct from MVP.
Unknown awards, other leagues and unconnected competition variants receive an
award coverage response, never a request to name the winner. Multiple awards
require choosing one. Predictions, voting explanations and unsupported filters
are withheld rather than silently discarded.

The selected season applies when omitted; explicit seasons override it. A bare
year means the season ending in that year. “Latest” means the latest reviewed
record for that award, not a guarantee of live coverage. Winner-only requests
use reference evidence without loading the warehouse. Their conversation context
retains the winner and season for performance follow-ups and supersedes stale
player selections or failed clarifications.

Player performance uses the existing governed overview and verifies the NBA
player ID and name against warehouse evidence (accent differences are accepted).
Regular season is the default for season awards; explicit playoff requests use
full playoff scope. Finals MVP lookup is supported, but Finals-only performance
is not: it retains the award answer and explains the missing game-round scope.
The user can explicitly request regular-season or full-playoff performance.
Whole playoffs are never silently presented as Finals statistics. Clutch award
lookup does not establish clutch statistics or explain voting; its default
performance overview is whole regular-season games.

A missing statistical source preserves the sourced winner with a partial-answer
limitation and never substitutes a different season. The catalog is reviewed
reference data, not model memory or runtime web scraping. Before adding records,
verify the official NBA winner list and player identity, retain provenance, and
run the catalog-wide regression matrix in `tests/test_award_lookup.py`.

Monthly performance overviews retain five selectable trends (PTS, REB, AST, STL,
BLK) when complete data is available. Visualization selection preserves these
views without a model call; missing metric observations still withhold that trend.

Browser recovery carries only an award key, season and performance phase. The
server reconstructs award evidence from the reviewed catalog and verifies the
saved winner identity. This preserves the Finals-only limitation across process
restarts; legacy award questions are re-resolved using their saved season.
Unverifiable award context is rejected rather than broadened to another phase.

Broad player performance questions (including “how did he perform/play/do” and
“how has he been doing”) use the full overview when no specific metric or
unsupported condition is requested. The generic metric planner cannot silently
replace basketball performance with a fantasy proxy. Fantasy queries require
explicit intent or a metric-preserving continuation of an existing fantasy
analysis; otherwise Ask requests clarification before executing the query.
Metric abbreviations and scoring/efficiency requests replace inherited fantasy
intent. Home/away qualifiers bypass the broad overview so their filters remain
part of the governed metric plan.

## Governed similarity and league references

Similarity and league-baseline questions use deterministic evidence handlers;
models do not write their numbers, tables or factual summaries. Similarity uses
one selected-season publication, validates identities and finite 0–1 scores, and
returns at most six neighbors. The score is `1 / (1 + Euclidean distance)` over
normalized features, not a probability or forecast. Its evidence retains a hash
of the returned publication; the serving response does not provide a model build
version. Custom dates, phases, metrics and other neighbor filters are withheld.

League references support one metric, one season, one phase (regular season by
default), and an optional last-N appearance window or explicit ISO date range.
“Recent” means the last ten recorded appearances. The reference pools all recorded
player appearances, including the focal player, across the same calendar window.
For last-N requests, the window begins at the focal player's earliest selected
appearance and ends at their latest selected appearance, even if the league has
played more games since then. Each player appearance has equal weight
for count averages; shooting ratios pool makes/attempts. This is deliberately
not the average of player averages or the older dashboard baseline table.

Evidence includes player and league values, components, sample counts, scoped
a peer-membership count and SHA-256 digest, source provenance, and the unrounded
player-minus-league change. The digest uses sorted `(season, game_id, player_id)`
tuples serialized as compact UTF-8 JSON. Full peer IDs remain reconstructible from
the source snapshot rather than being copied into browser chat history.
Missing components suppress the change; partial individual averages remain
labeled with their complete-input counts. Ratios differ in percentage points.
A metric-only follow-up recalculates the same reference scope, including browser
context recovery. Recovered identity hints are validated when a follow-up inherits
them; a new explicit player question starts from its own identity. Unknown
conditions never fall back to an unfiltered answer.

`app/agent/routes.py` is the ordered dispatch registry. The first matching route
wins; semantic subroutes reuse its precedence after resolving conversation scope.
Each route declares its handler, capability and evidence contract. A shared
payload builder supplies common fields while preserving route-specific statuses
and evidence. The older adapter remains only for repositories without a governed
warehouse; the production BigQuery path does not fall back to it on errors.

Reference routing recognizes “nearest comps” and “alike” as similarity requests.
Participation, research and published-study intents retain priority over incidental
reference wording (for example “similar minutes when a teammate is out”). Explicit
peer-similarity filters such as “similar players at home” are still refused rather
than replaced by unrelated research. New questions clear a pending reference
clarification; short name or metric replies can resume it. Routing and rendering
share the same captured conversation context.

Reference handlers own visualization selection and call no chart-selection model.
Unexpected source errors use the normal agent error response. Non-warehouse
compatibility repositories retain other legacy workflows, but similarity and
league references deliberately require a governed source and refuse when absent.
