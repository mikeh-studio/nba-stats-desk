# Ask workspace

Every tab opens on the latest supported season (currently 2026–27). There is no
shared season selector; old page links with a season parameter redirect to the
latest view. Explicit historical API requests remain supported.

Ask accepts historical seasons in the question, including “last season.” Without
a stated period it starts with the latest season and checks supported archives
newest first when season evidence is unavailable. Available teammate studies use
their published date window. Answers disclose an earlier-season fallback and its
scope. Explicit seasons/dates and conversational follow-up scope are preserved;
permission errors and invalid evidence never trigger fallback. This does not
create missing studies or infer an injury absence from a missing appearance.
Research breakdown fallback requires observations for every requested player in
the same candidate season and phase. Availability fallback checks the named pair
against the candidate comparison; unrelated rows or an entirely unverified pair
do not establish coverage. Uncovered seasons are skipped before resolving aliases
against team context; ambiguous names within a covered season still require
clarification. A small verified sample stays visible with its limitations rather
than being replaced solely for being small.

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

## Factual-output reachability

| Configuration / route | User-visible factual output |
| --- | --- |
| Default `BigQueryWarehouseRepository`, or an explicitly supplied semantic warehouse | `StatsAgent` selects governed metrics, overview, comparisons, splits, reference, availability or research handlers. Their statistics and factual summaries are deterministic; failed governed requests do not enter compatibility narration. |
| `RESEARCH_STUDIES_PATH` / research snapshots | Published catalog and breakdown handlers validate scope and render from evidence. Optional visualization chooses a verified chart candidate, not new values. |
| `AGENT_TEAMMATE_STUDY_PATH`, when not replaced by that pair's published catalog entry | `teammate_study_v1` uses the model only to select scope. The server renders the frozen assists study, table and uncertainty notice. |
| Injected non-BigQuery repository with no semantic warehouse | The final `legacy_repository` route retains `_answer_from_evidence` and the compatibility tool loop in `service.py`. These can publish model-authored prose/tables, including streamed prose. External usage is unknown; this route is not universally fact-governed. |

The v1 study rejects missing/nonfinite estimates, invalid sample counts, nominal
p-values outside [0, 1], malformed intervals and inconsistent identity/date scope.
The narrative distinguishes original descriptive, complete-case raw and adjusted
estimates, all Out minus participated. It makes no validated significance or causal
claim. Imported limitations remain visible; no live refitting or warehouse refresh
is implied. Scope selection still requires correct model interpretation for requests
without literal date/season/phase guards; deterministic narration does not prove universal
natural-language scope accuracy.

The additive `study_claims` field (`teammate_study_claims/1`) binds each displayed
value to its source JSON path, study ID and content digest, full identity/date/phase
scope, metric, unit, contrast, qualifier and display paths. Values are imported
frozen estimates, not arithmetic recalculated by Ask. The exporter separately
verifies its analysis inputs. The digest identifies content and does not certify
source quality. Existing saved payloads need no new field to render. No changes
are made to `league_baseline/2` or published similarity semantics.

Only allowlisted scope fields are sent to the scope-selection model or returned
in scope/claim metadata. Unknown scope keys are not exposed. Model-supplied extra
answer fields are ignored defensively; normal provider responses contain scope
selection only. Malformed configured artifacts fail closed for potential study
questions, including natural-name absence questions, rather than falling through
to compatibility narration. Invalid JSON shapes, noncanonical dates and nonfinite
values are rejected before any provider call.

Version 1 assumes the exporter contract: assists per game, reported Out with no
appearance minus participated; adjusted estimates and counts use complete cases
and OLS with pregame covariates and calendar-month effects. The checked exporter
`export_teammate_study.py` enforces the fixed specification. Importers must preserve
these invariants; Ask cannot reconstruct methodology from the compact artifact.
Claims disclose `contrast_basis: declared` when `difference_direction` is present
and validated, or `v1_default_assumed` when omitted for v1 compatibility. Units and
method qualifiers come from this versioned contract, not independent fitting.

The digest is SHA-256 of the parsed full artifact serialized using Python JSON
with sorted keys, compact separators, `ensure_ascii=True`, and finite numbers.
It identifies normalized content, not the original bytes: duplicate object keys
collapse during parsing and equivalent float spellings normalize. Display rounding
does not change the bound values. A point estimate rounding to zero is described
as such, never proof of no association; nominal p-values below 0.001 display as
`<0.001`.

## Two-player comparisons

Explicit `A vs B`, `A versus B`, and `Compare A and B` questions use a
deterministic two-player scorecard. So do evaluative questions that name two
players but no statistic (“Who played better last season: A or B?”, “Who's
better, A or B?”, “Is A better than B?”, “A or B: who had the better season?”);
a named statistic (“who scored more, A or B”) stays with the metric planner. Nicknames use the existing source-backed
alias catalog. Each side is resolved separately; ambiguous identities require
selection and already-resolved sides are retained during clarification.
Across all Ask routes, when a player name does not match exactly, deterministic
spelling similarity offers up to five players from the loaded source evidence.
Full-name matches take precedence over partial names. A full-name typo resolves
automatically only when exactly one token of at least four letters differs by
one insertion, deletion, substitution or adjacent transposition, the mean token
similarity is at least 0.90, and the lead over the next credible identity is at
least 0.08. These are conservative spelling heuristics, not probabilities.
An omitted generational suffix (Jr., Sr., II, III, IV) also resolves when its
full base name identifies one credible source player; competing base names
require clarification. The result names the player used in the API answer,
including when the resolved scope has no observations or unsupported coverage.
Overviews and comparisons also show the correction in their scope notes.
Partial names and competing identities require confirmation, including duplicate
canonical names. With no credible match, the response is `not_found` with no
statistics; unmatched full names do not fall back to just their first/surname.
Confirmed comparison sides are retained while the other side is clarified.
Appearances, splits, similarity, league baselines, planned metrics/game logs,
research, teammate studies, and compatibility tools share this policy. Name
preflight uses the route's loaded roster before planning or query execution;
it never fetches an unrelated warehouse just to identify a player. Coverage
checks still apply first when a source is unavailable. Generic award/leader
questions need no player matching. Only name spans are canonicalized: roles,
metrics, dates, phases, and other constraints remain intact. Clarification
choices are revalidated against source IDs when the original query resumes.
A teammate's team is not used to silently break an identity tie. Choices show
player names and source-backed team abbreviations. IDs remain internal, and the
answer identifies the user's selection by name and team. Candidate teams come
from the relevant source season when available. Indistinguishable name/team
labels ask for a narrower season or team instead of displaying identical buttons.

Identity clarification leads with the confirmation question. Source-coverage
disclosures remain in response metadata and appear with the completed answer.
Other scope clarifications retain visible coverage notices. Identity failures
retain source-query metadata, tool completion, and actual model-call counts.
Appearance correction and selection notices are attached before claim binding
and remain valid under enforcement; numeric answer mutations still fail.
`Compare A to B`, `How does A compare with B?`, and a performance question
followed by `How does he compare to B?` also use this scorecard when A is named
in the same question. Both players share the question's stated dates and phases.
An unresolved second identity requires clarification; it cannot become a
single-player overview. Temporal baselines such as `compared with last season`
remain period comparisons.
The scorecard shares one date/phase scope and one qualified cohort per metric
across points, rebounds, assists, steals and blocks. Minutes and sample sizes
come from the same appearances. Differences use unrounded averages; missing
components withhold edges and percentiles rather than becoming zero.

Unavailable winning-impact measures are omitted from the comparison UI and
analysis. The governed player-game source has no connected Win Shares or on/off
possession totals. Comparisons publish an empty impact list; the browser hides
null-valued legacy rows, obsolete missing-feed disclosures, and the unavailable
net-rating chart option, including when restoring older saved comparisons.
A future verified impact metric is displayed only when both players have numeric
values in the matching scope. This change does not connect locally scraped data
or substitute raw plus/minus for net rating.

The comparison result opens with compact player cards and a verdict, then three
summary areas: **Offense** (points and assists, with per-36 and true-shooting
rows from the detail sections), **Defense** (box-score rebounds, steals and
blocks only) and **Availability** (games and minutes, which measure playing time,
not health). Each area has a lean meter. Offense and Defense average the
percentile gaps of complete metrics (under 5 points is even; 30 points fills the
meter); Availability compares total minutes, or games when minutes are
incomplete (under 5% is even; 25% fills it). Up to three takeaway cards name the
favored player and cite their numbers. This browser text is derived from the
comparison evidence, like the earlier category takeaways; the verdict states that
box-score differences do not establish an overall winner. A per-game
head-to-head table replaces the per-stat bar chart: bar length is each player's
league percentile, the leader is bold with the unrounded-average margin, and its
values, percentiles and margins carry claim bindings.

The comparison includes three additional sections, collapsed under the
head-to-head table, using the same scoped appearances as the core scorecard: shooting efficiency (FG%, 3P%, FT%, eFG%, TS%),
ball security (turnovers, assists and minutes per game), and points/assists per
36 minutes. Shooting percentages use combined makes and attempts; differences
are percentage points. Per-36 rates use total production and total minutes,
not averaged game rates. TS% uses the 0.44 free-throw approximation.

Each row retains valid/observed games, missing-component counts and source
components. Partial values disclose their sample and suppress comparison edges.
Zero attempts or minutes produce unavailable rates, not zero. Server-built
comparison prose uses unrounded differences and describes higher/lower values
rather than declaring an overall winner. Turnovers favor the lower value, but
role and playing time remain relevant. Saved comparisons created before these
sections were added retain their original statistics; asking again retrieves
new sections from current evidence.

[Daily player comparison maintenance](operations.md#daily-player-comparison-updates) checks fresh
league-wide inputs and permits publication only after an isolated rebuild and
transactional baseline checks. Saved answers retain their original evidence.

Supported scope is a shared date range and regular season, playoffs, or both.
Explicit playoff years resolve to the corresponding season. Round, clutch,
opponent, home/away, wins-only and per-possession/per-minute filters are not
silently approximated. Period-versus-period questions keep their existing route.
Saved comparison context includes both full names and explicit dates. Fill-only
follow-ups preserve that scope. Narrow-screen tables scroll horizontally.

The shared header puts Search players beside the brand at the upper right, with
four navigation links: Ask, Performance, Trending, and Profiles. Player profiles
remain available from search and existing player URLs. Search stacks below the
brand on narrow screens.

The new-question composer and model settings sit above the analysis tabs and
remain visible while browsing history. The question remains in the top composer
during and after submission. Repeating the same question with the same season,
provider, and model reuses its completed answer without another request or turn,
including after reload and after follow-ups. Failed requests can be retried.
Changing the question or settings creates a separate analysis on submission
(an untouched draft is reused). Comparison uses the trimmed question and current
setting values; changing a setting back before submitting counts as unchanged.
New chat opens an empty composer. Switching tabs restores their question drafts
and settings; submitted search snapshots survive page reloads.
The bottom follow-up composer appends to the selected conversation and retains
its validated identity/date/phase context. The compact composer shows report scope
and an accessible send button. Both question bars use a 52px minimum-height,
14px-radius grid container with a 28px arrow button, a centered 16px SVG, and 12px right/bottom insets. Textareas grow with
wrapped text and shrink when cleared; the button occupies its own column.
Suggestions fill the follow-up composer without submitting. A conservative
browser vocabulary check admits common refinements, names in the report, and
terms in the opening/current report question so clarification replies remain usable.
Exact report suggestions also pass, including suggestions with unfamiliar words;
edited suggestions use the normal check. The follow-up composer is hidden when
the chat has neither a usable report nor a pending clarification. A failed
follow-up retains the prior report context and allows an explicit retry.
Unrecognized wording stays in the composer and asks for clarification or “Open in
new tab”; that action submits with a new conversation ID and no previous context.
This navigation aid is not semantic identity validation: novel related wording may
need rephrasing. The backend still validates identity/scope and fetches evidence.
Editing or switching tabs clears the prompt; prior reports and drafts remain.

Ask uses one tab per conversation. Five tabs are visible on desktop, including
the new-question draft. At smaller widths, fewer tabs are shown; More remains
available. Opening a sixth chat moves the oldest open tab out of the strip. It
does not delete the conversation. Closing a tab has the same non-destructive
storage behavior. Follow-ups remain in their original conversation.

More opens searchable local history. Search includes opening questions and
follow-ups, and the local endpoint pages beyond the browser's recent cache.
Opening a result restores the stored response payload without rerunning queries.
The latest saved response with an unsuccessful status is labeled as saved and
offers “Run this question again.” Only clicking that action submits a new request;
it appends the new result in the same conversation and preserves the old response.
All questions and responses remain visible in chronological order, with each
response owning its profile, methodology, tables, and interactive charts.
Follow-ups are read by scrolling; there is no question-review dropdown.
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
up to three visible Key Takeaways, eleven primary metrics (six or nine for focused
profiles) and an adaptive performance chart. The server-written Overall sits
above icon takeaway cards and the compact metrics table; the performance chart
is collapsed until opened. The chart starts with the requested
focus metric, or PTS for general overviews; PTS/REB/AST/STL/BLK
controls switch the series. Other Ask intents retain their original answer and
table/chart renderers instead of receiving invented overview insights.

Broad overviews select up to three evidence-backed findings rather than narrating
every metric. Scoring links personal-baseline changes with shot volume, true
shooting and per-36 rates; creation pairs assists with turnovers and peer standing.
A remaining slot may explain a material rebounding change or notable qualified
steal/block production. These are recorded events, never defensive grades.
Every category is considered; unchanged low-signal values remain in the table.
Findings lead the copy; activity can be a supporting sentence. A final compact
scope line retains sample counts, baseline dates and distinct cohort qualifications.
Sidebar bodies pair each conclusion with its decisive supporting contrast.
Peer comparisons require the current qualification policy and matching distribution
cohort. Scope and baseline are explicit. Partial or thin evidence never creates
comparisons; complete thin samples retain current values and a short sample note.
Generic lists of unmeasured capabilities are omitted. Direct unsupported questions
still receive an honest answer. Saved responses retain their original prose.

Private backend events distinguish explicit capability requests from incidental
evidence gaps and overview denominators. They contain allowlisted codes, request
IDs, scope and evidence/version references, not raw questions or answers. See
`docs/overview-insights.md` for monitoring and privacy boundaries.

“Non-scoring stats,” “contribute/contributed beyond scoring,” “besides scoring”
and “other than scoring” select the contribution profile before generic scoring
matches. All-around/all-round requests select a broad overview. Multiple named
focuses and explicit metric pairs retain semantic planning/clarification instead
of silently becoming a single-focus overview. Focused scoring, playmaking,
rebounding and defensive questions remain focused.

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
value withholds the distribution. Scoring-distribution prose is reserved for the
scoring focus, freeing the broad assessment for non-scoring coverage. Samples
below five games carry a caution.
Narrow metric requests, awards and unsupported requests remain concise. No-data
responses do not pad out three paragraphs. All detailed metrics remain in the
tables and evidence, with their original units.

The overview describes scoped category production, game-level variation, and
box-score activity, with additional minutes, attempt, shooting-efficiency,
turnover, and per-36 context. These metrics do not establish causal drivers.
Opponent and selected teammate summaries are available in the offline evaluation;
see [Player context](pages.md#player-context) for integration and coverage limits.
Thin percentile bars use native meters. Icons are vendored Tabler assets under
their MIT license; the existing locally hosted Barlow fonts and real player
headshots are reused.

See [Players and research breakdowns](pages.md#players-and-research-breakdowns) for shared detailed queries, multi-stat teammate studies, and versioned pregame context.

Teammate comparisons default to regular season plus playoffs for the selected
season. A December-ending pilot is not a full-season fallback. Published studies
must bind their phase and window; explicit dates or phases cannot reuse a
differently scoped result. See [study scope](pages.md#players-and-research-breakdowns) for full-season
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
definitions. Opening a saved chat shows its latest response details; starting a new
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
also blocks availability conditions, so a model cannot discard them. Overview and
two-player scorecard scopes independently reject absence cues (“when B is out”,
sat out, missed, inactive, DNP, “without his teammate”) with a clarification,
including after player selection, rather than returning a full-period summary.
Absences named after their subject (“in games B missed”) route to this
comparison, while “missed shots” does not. Naming two players as teammates is not
itself a condition. The old published-study route remains compatibility support when dynamic evidence is not
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

Performance-over-time overviews retain five selectable trends (PTS, REB, AST, STL,
BLK) when complete data is available. Visualization selection preserves these
views without a model call; missing metric observations still withhold that trend.
Windows up to 31 days show game dates; longer windows show monthly averages.
See [metric labels, samples and calendar scope](pages.md#ask-overview-metrics-and-samples).

Browser recovery carries only an award key, season and performance phase. The
server reconstructs award evidence from the reviewed catalog and verifies the
saved winner identity. This preserves the Finals-only limitation across process
restarts; legacy award questions are re-resolved using their saved season.
Unverifiable award context is rejected rather than broadened to another phase.

Broad player performance questions (including “how did he perform/play/do”,
“how has he been doing”, “how was/how good was X”, and “what kind of season did
X have”) use the full overview when no specific metric or unsupported condition
is requested. Real box-score statistics are the default: fantasy proxy metrics
are offered to the metric planner only when the question asks for fantasy (or a
box-score index) or continues an existing fantasy analysis, and a proposed
fantasy metric without that intent is withheld. Fantasy roster advice requires
fantasy wording or a first-person roster move (“should I drop X”); “drop-off”,
“drafted” and similar real-stat wording never routes there. Planner and engine
invariant failures (an invalid plan, an incompatible comparison, failed evidence
validation) keep their status code for traces and evaluation, but the reader sees
a plain-language message rather than the internal error text.
Metric abbreviations and scoring/efficiency requests replace inherited fantasy
intent. Home/away qualifiers bypass the broad overview so their filters remain
part of the governed metric plan.

## Governed similarity and league references

Statistical Profile Similarity and league-baseline questions use deterministic evidence handlers;
models do not write their numbers, tables or factual summaries. Similarity uses
one selected-season publication, validates identities and finite 0–1 scores, and
returns at most six neighbors. The score is `1 / (1 + Euclidean distance)` over
normalized features, not a probability or forecast. Its evidence retains a hash
of the returned publication; the serving response does not provide a model build
version. Published-profile custom dates, phases, metrics and other neighbor filters are withheld.

Explicit last-N questions use a separate `recent_statistical_similarity/1` contract.
They default to PTS/REB/AST per-game averages for each player's own last N recorded
appearances in one season and phase (regular season by default; 2–20 games).
Explicit subsets of these metrics are supported; other constraints are not dropped.
Only complete N-game windows qualify. Population standard deviations across the
eligible cohort, including the reference player, scale each metric equally. Rank
by RMS standardized distance; display `1 / (1 + distance)`. Constant metrics
contribute zero distance and ties use player ID. Responses disclose the default,
individual sample dates, cohort and source cutoff, and invite clarification.
Metric follow-ups retain identity and window. This is a descriptive comparison,
not the published 48-feature model or a playing-style claim.

The `/similarity-methodology` page explains inputs, missingness, scoring, grouping
model selection and PCA limitations. Scores across the two methods are not
interchangeable.

NBA Finals requests first check the narrow, sourced `participation_catalog.json`.
A matching verified nonparticipant returns `not_applicable` with reference evidence
and no fabricated zero statistics. The initial record covers Stephen Curry in
2025–26, sourced to the NBA elimination report. Unknown players/seasons and other
rounds retain an explicit coverage limitation. Missing box scores or a current
team are never sufficient evidence of nonparticipation.

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

Every Ask payload carries `ask_route: {route, source, reason_code}`
([`app/agent/route_labels.py`](../app/agent/route_labels.py)). `route` names the
answer path: `player_overview`, `player_comparison`, `player_split`, `award`,
`governed_metrics` (the metrics planner), `similarity`, `league_baseline`,
`availability`, `appearances`, `research_breakdown`, `teammate_study` (v3
catalog), `teammate_study_v1`, `research` (research handler reached, no sub-route
chosen), `legacy:<sub-route>`, and `unsupported`/`clarify` for decisions made
before any handler. A handler's own refusal or clarification keeps its label and
sets `reason_code`. `source` is `deterministic`, `prior_route`,
`pending_clarification` or `model` (the research planner chose the sub-route).
The label is recorded on the request trace (`ask_route` in the service log),
stored as `route` in successful turn context and on pending clarifications
(which also keep the offered candidates' player ID, name and team), and
withheld from planner prompts. Logs carry labels and reason codes, never question
text. Older stored turns and clarifications without a route remain readable.

Reference routing recognizes “nearest comps” and “alike” as similarity requests.
Participation, research and published-study intents retain priority over incidental
reference wording (for example “similar minutes when a teammate is out”). Explicit
peer-similarity filters such as “similar players at home” are still refused rather
than replaced by unrelated research. New questions clear a pending reference
clarification; short name or metric replies can resume it. Other untyped
pending clarifications are cleared by a self-contained request for a different
task (a game log, a leaderboard, another metric, a teammate absence or research);
it routes on its own wording instead of being folded into the pending question.
Name replies (“Show me Avery Example”), scope replies, thresholds and
restatements of the same task (“How is Avery Example performing?” for a pending
overview) still resume the pending request and keep its scope. A name reply must
match the clarification's stored identity candidates (name, team abbreviation or
ID), or, when none were stored, a name in the pending question; a short reply
naming another player is a new question. Legacy clarifications keep resolving
name replies against the full roster. Typed Overview clarifications keep their restatement contract: the reply's
wording is used and only missing dates or phase come from the pending request.
Routing and rendering share the same captured conversation context.

Reference handlers own visualization selection and call no chart-selection model.
Unexpected source errors use the normal agent error response. Non-warehouse
compatibility repositories retain other legacy workflows, but similarity and
league references deliberately require a governed source and refuse when absent.

Availability questions accept “how many points … average”; award lookups accept
“Name the MVP winner” and “Who received MVP”. These wording variants preserve
the same bounded scope checks; unsupported additional constraints still refuse.


## Request budgets and execution traces

Semantic planning, research planning, legacy planning, narrative generation and
optional chart selection use `app/agent/execution.py`. Each `StatsAgent.answer`
gets an isolated context-local budget, default **120 seconds**, configured with
`AGENT_REQUEST_DEADLINE_SECONDS`. Planning, evidence loading, calculation,
rendering, model attempts and exponential backoff consume that same budget.
Each provider attempt receives the smaller of its configured timeout and the
remaining request budget. Legacy route planning also retains its tighter
`AGENT_PLANNER_TIMEOUT_SECONDS` cap. Semantic planning no longer has a fixed
45-second timeout. Provider timeouts are SDK I/O timeouts, not a guarantee that
an entire streamed response finishes within that duration.

`OPENAI_AGENT_MAX_RETRIES`, `ANTHROPIC_AGENT_MAX_RETRIES` and
`OPENROUTER_AGENT_MAX_RETRIES` independently configure retries (default two;
zero means one attempt). The shared backoff base remains
`OPENAI_AGENT_RETRY_BASE_DELAY_SECONDS`. Only connection/timeouts, HTTP
408/409/429 and 5xx errors are retryable. SDK retries are disabled on every
client clone. Retry sleeps must fit the remaining budget, and the next attempt
receives a freshly reduced timeout. Validation and authentication errors are
not retried. After any Claude text chunk has been received, a failed stream is
never replayed. OpenAI and OpenRouter responses currently remain buffered.
Live evaluation explicitly sets all three retry limits to zero in addition to
its global attempted-call budget.

The deadline is **cooperative**: checks run before and after instrumented stages,
between Claude stream chunks, before provider attempts and after returns.
BigQuery query submission/result waits are capped by the remaining budget.
Blocking Python work, a warehouse lock wait, an SDK read, or result-page iteration
cannot be forcibly interrupted here; a late return is rejected before downstream
work or a successful response. Successful conversation turns and pending
clarification updates are staged during execution. Reads within that request
see its pending clarification overlay; other requests see only committed state.
The full answer, including optional chart enrichment, must pass the final budget
checkpoint before the staged in-memory changes are committed together. A failure
before that success boundary retains the conversation/clarification state as it
was when governed agent execution began. Restoration from saved history or
bounded browser hints runs after admission, before this execution boundary. It
can set or clear a pending clarification or append recovered context; those
restoration changes are not rolled back if restoration validation or the
subsequent agent execution fails.
They do not constitute saving an answer to the failed question.
Failed agent executions do not write local answer history. In-memory finalization
and the separate local history file write do not form an atomic storage
transaction. The deadline does not forcibly terminate blocking work or bound
response-body delivery time.

JSON and SSE share a per-process admission limit. With
`AGENT_MAX_CONCURRENT_REQUESTS` unset, the limit follows the running AnyIO
worker capacity (normally 40); a positive override selects an explicit limit.
This preserves the framework's existing worker ceiling and does not claim that
40 concurrent model/warehouse requests is an optimal production setting.
At capacity, requests receive a safe HTTP 503 response before Ask execution.
An active season/conversation also permits only one request at a time, preventing
competing context updates; overlapping requests receive a safe HTTP 409.
Request-rate checks precede admission: 409/503 capacity rejections still count
against the minute/day request quota. This preserves the anti-abuse limit on
attempts; a busy response does not waive it. The browser never retries silently.
A capacity slot remains occupied until both the actual worker finishes and the
JSON/SSE response send closes, including when a disconnected client stops
waiting. This keeps unsent complete terminal payloads within the active-request
count. A stalled response-body send can hold a slot beyond the computational
deadline: no additional transport timeout is assumed or introduced here.

SSE stores serialized interim frames in a nonblocking mailbox, bounded by
`AGENT_STREAM_BUFFER_BYTES` (**1,650 bytes**) and
`AGENT_STREAM_BUFFER_EVENTS` (**9 events**). These defaults are the measured
maximum complete interim output from the 124 public offline cases (145 SSE
turns), with no assumed production headroom. They are regression-derived
delivery budgets, not tuned provider or user-network limits. When either quota
would be exceeded, the stream stops adding interim progress and answer deltas
for that request; the complete governed final answer still arrives. Existing
queued frames retain their order. The final/error event has a separate single
terminal slot, so it cannot deadlock behind a full interim mailbox and its
evidence is never truncated to fit the interim quota.

This bounds retained interim delivery data, not total request memory. One full
terminal payload, the agent's evidence/model buffers, and a frame currently
being serialized or sent remain outside that quota. A disconnected SSE consumer
clears the mailbox and suppresses future delivery events. JSON/SSE workers can
finish within the existing cooperative deadline and retain a successfully
completed answer under the existing conversation/history rules. There is no
disconnect grace timer, automatic resubmission, or new reconnect protocol.
Retrieval after leaving the page still depends on existing persistence settings.
Raw JSON sockets may not report detachment to the application; a failed send or
closed response releases the delivery side of its capacity slot when observed.

Service summaries include attempted `model_calls` (including failures),
`provider_failures`, and `stage_latency_ms` for executed routing, identity,
planning, evidence, calculation, rendering and generation boundaries. Absent
stages were not instrumented/executed, not measured at zero cost. Timings are
inclusive within each stage; nested same-name spans count once, but different
stages can overlap and should not be summed into total latency. Total request
latency remains separately recorded. This instrumentation covers the governed
paths and model calls; legacy repository tool durations also remain in existing
tool records. No question, tool arguments, evidence, or provider exception body
is added to the log.

The safe summary also records the admission limit/count, interim backlog peaks,
suppressed-event count, overflow, and observed delivery detachment. These values
are a snapshot at worker finalization. A disconnect or stalled send occurring
afterward is not reflected in that already-emitted summary; they do not measure
the full transport lifetime or reliably classify every raw JSON disconnect.

Terminal traces distinguish `request_timeout`, `provider_timeout`,
`provider_failure`, `invalid_plan`, and scope refusals. Legacy planner failures
can still recover through its deterministic fallback; failed attempts remain
counted and the final outcome describes the resulting answer. Optional chart
selection gets one attempt within a separate budget of at most 10 seconds,
reserving one second of the request budget for finalization. If that reserve is
already reached, model selection is skipped. A timeout or provider failure falls
back to a deterministic chart while the overall request deadline still applies.
As with other stages, a blocking transport that ignores its timeout can still
exhaust the overall deadline.
Context restoration validation failures return HTTP 400 on JSON. Once admitted
to SSE, the response starts with HTTP 200 and ends with one safe `error` event
instead of a `final` event; clients must inspect that terminal event. Both paths
retain `X-Request-ID`, release capacity after delivery closes, and record an
`HTTP_400` error type in the operational summary.

HTTP/SSE provider failures keep the existing safe generic error response, while
the server-generated request ID links to the private operational summary.

## Compact metrics and balanced contributions

The percentile cell opens a compact metric drawer: **Among players** uses the
exact qualified comparison cohort, and **Across games** shows individual recorded
appearances, median and spread. Shooting observations include attempts; the game
selector supports touch and keyboard use. The drawer overlays the page without
expanding metric rows or making additional Ask requests. See the
[distribution contract](pages.md#metric-distributions) for
qualification, missingness, quantiles and bounded observations.

Broad overviews use eleven primary metrics; focused profiles use six or nine with
expandable additional metrics. Per-metric sample/opportunity notes and balanced,
server-authored takeaways replace browser-selected strongest-percentile claims.
See [overview metrics and sample policy](pages.md#ask-overview-metrics-and-samples) for supported
focuses, scope guards, units, comparison eligibility and missing-data behavior.


## Conversational time defaults

Relative periods are resolved once. The planner receives the resolved wording in
both its question and current-question fields, so “last year” cannot be shifted
again relative to the already-selected season.

Ask resolves casual time phrases deterministically before overview, metric, and
comparison planning. “Last year”, “last season”, “previous year” and “prior season”
mean the last completed NBA season under the repository's July–June season bounds.
The request date (or explicit as-of date) anchors this interpretation, independently
of source freshness. “This year” means the NBA season containing that date;
explicit season selection and established “this season” behavior remain supported.
“Past year” and numeric year windows mean rolling calendar months. “In 2025” means
calendar 2025; current calendar years end at the request date. Month names without
years inherit a single conversation season when available, otherwise use their
latest occurrence. Recently/lately overviews use up to ten recorded appearances
in the selected season/phases, with the preceding appearances in those same
phases as their baseline. Metric calendar windows also honor an explicit as-of
date when resolving relative periods and clipping the requested month.

Successful inferred scopes show a compact assumption above the analysis. Last-year
answers offer calendar-year and rolling-12-month correction buttons that submit
explicit replacement questions. Saved answers retain these controls. Existing
phase defaults, metric selection, ambiguity guards and missingness semantics stay
in effect. Follow-ups retain resolved dates unless new time wording overrides them.
Conflicting periods still require clarification; unavailable evidence is reported
without silently replacing the inferred period or inventing values.


### Calendar metric coverage and accessible ranks

Yearless months first use an explicit season or playoff year in the question,
then the primary conversation season (derived from its start/end when comparison
archives are also listed), otherwise their latest occurrence at the earlier of
the context end and request/as-of date. A rolling context spanning multiple
seasons cannot infer a future month from its end date. Season and recent-appearance overviews cap their
end at an explicit as-of date; recent historical requests use the applicable
available season. Off-season/start-of-season recent requests can include the
preceding available season while preserving their upper date and phases.

Fresh metric calendar windows load every overlapping available archive and bind
those seasons to the calculation, just as inherited windows do. Resolved rolling
year requests become explicit dates anchored to the request/as-of date. Ordinary
source-anchored numeric-month queries retain their established semantics. A
missing required supported archive withholds the answer; dates conflicting with an explicit
named season require clarification. Source coverage/freshness remains visible.
Complex month comparisons and since/before/after ranges retain the validated
planner path. A dated month on the right side of a comparison is a period, not a
second player identity. Inferred planner month/range ends are clipped to today or
the explicit request anchor and disclosed; explicit user future ranges still
require clarification. Windows beyond the newest supported archive retain source
coverage warnings and do not establish complete coverage of later seasons.

For a named player's dated points summary, omitted aggregation means points per
game. Under `semantic_intent/3`, the server derives omitted aggregation from the
validated request and chooses average even when the planner proposes total or
mislabels its source. Explicit or inherited value contradictions still fail.
Canonical date intervals and calendar scope are validated before execution. Assumptions and
`query_plan.aggregation_policy` disclose this choice. Explicit totals stay totals,
explicit averages have no default disclosure, and partial follow-ups preserve a
compatible previous aggregation. Fully restated named metric/time requests, including season requests, can start
fresh; the omitted-aggregation default remains limited to dated PTS summaries. Identity and scope contradictions fail before literal-scope replacement.
Zero points count as an observation; missing points remain unknown. Canonical
unsupported-intent guards do not establish arbitrary-language accuracy. See the
[semantic contract](semantic-contract.md) for scope and sample semantics.

Distribution buttons announce both the percentile or qualification status and
the descriptive qualification note. Focused prose uses full statistic names such
as three-pointers made and turnovers; metric tables keep their abbreviations.

Implicit position context may fall back to an explicitly labeled league reference.
An unavailable explicitly requested group is partial and never replaced by league
data. Supported comparison clauses,
shared focused summaries and peer-evidence details follow the
[position policy](overview-insights.md#position-comparisons-and-focused-profiles-v3).


### Overview variation and requested-peer fulfillment

See [question-first comparisons and variation](overview-insights.md#question-first-comparisons-and-variation-v4)
and [request fidelity](overview-insights.md#request-fidelity-and-review-regressions)
for the shared contract. An unavailable explicitly requested cohort is partial,
never an answered league substitute. Game spread and dated highs use the requested
metric and full scoped sample; all displayed comparisons retain their reference.


### Research intent contract

The `research_intent/3` planner chooses affirmative `descriptive`, registered
`study`, or `unsupported` intent. Study schema variants bind the exact pair,
focal player and exposure player. A malformed study never becomes a breakdown.
The model's `phase_source` label is advisory only. The server derives
explicit, inherited or default phase from the question and applicable context.
When neither explicit nor applicable prior phase exists, null and the correct
intent default are accepted regardless
of that label: Regular Season for descriptive requests and Both for studies.
An applicable prior phase, including a same-intent default, must be copied as a
concrete value. Explicit/inherited null is refused. Values must match the resolved scope; invented nondefault
values are rejected. Saved context records the server-derived source, whether
phase came from the user, and requested dates. On an intent change, a prior intent
default and published study dates are not treated as user-selected scope;
explicit user scope remains inherited.

Shared literal guards check known identities, season, date, phase and venue,
recognized metric and aggregation cues, and supported intent. Executable queries
still use deterministic validation and evidence arithmetic. These guards do not
parse arbitrary natural language: unrecognized modifiers still depend on correct
model interpretation, including opponent and rest paraphrases. Failure to match a
literal cue is not proof that the user omitted that filter. Unnamed follow-ups
retain prior player/exposure identities; new identities require recognized names.
Per-36 units require an explicit cue or an unchanged inherited metric scope. Controlled JSON/SSE tests establish fixture behavior, not
live model quality. Old planner prompt/schema recordings are incompatible with
this version and cannot establish its accuracy.

## Exact presentation bindings

See [claim bindings and surface inventory](ask-claim-bindings.md) for the additive
`ask_claim_bindings/2` envelope. Structured cells and selected scope/sample prose
link exact sources to exact destinations. After visualization enrichment,
`ASK_CLAIM_ENFORCEMENT=enforce` withholds contradicted bindings; default `log`
records invalid reports while preserving content. See the [observation rollout](ask-claim-bindings.md#observation-rollout)
for activation and loss boundaries. Unbound surfaces remain explicitly incomplete;
existing non-binding safeguards remain enforced in both modes.

## Agent serving preparation

See the [Ask serving contract](ask-serving-contract.md) for source-certified
publications, the metric inventory, opt-in Phase 2 routing, request telemetry,
and parity checks. Routing and deterministic planning remain off by default.
Activation requires the contract's fresh measurements and explicit rollout gates;
local and fixture validation do not establish production readiness.

## Behavior documentation and ownership

This guide is the authoritative app behavior reference; the
[claim guide](ask-claim-bindings.md) defines verified surfaces and known gaps.
The [ownership audit](intelligence-ownership-audit.md) connects those contracts to
public owners, model-call boundaries and regression coverage. Optional intelligence
packages supply planning through v2, which has no answer-generation API. Bounded
public scope/chart selection and the explicitly non-governed legacy narrator keep
the restrictions described above. Package-operation guides should link here rather
than maintain a second app-behavior specification.
