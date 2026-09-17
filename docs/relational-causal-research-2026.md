# Relational data and causal impact: research and architecture decision

Researched 2026-09-16. Proposal only; no warehouse models, estimators, or model
runs were changed. Publication dates below are distinct from crawl dates.

## Recommendation

Keep BigQuery/dbt for governed facts, dated relationships, reusable features,
and analysis outputs. Make `player_game_context` a derived mart. Define causal
studies separately from warehouse joins. A relationship between two player IDs
does not itself establish how one player's absence changes another's outcome.

The useful separation is:

1. What happened, who was connected, and when we knew it.
2. Which comparisons and causal pathways a study assumes.
3. What the data and estimator support, including uncertainty and limitations.

## Recent primary-source findings

### Columbia Causal AI Lab: relational structural causal models

[Ejaz and Bareinboim](https://arxiv.org/abs/2606.14892), submitted June 12, 2026,
revised August 22; listed as ICML 2026. Models variable objects and relations,
with identification criteria for queries over new object combinations.
Relational neural models are evaluated in simulated traffic scenes. This is
research evidence, not an NBA deployment. It motivates preserving changing
rosters while explicitly stating assumptions about generalization.

### DoorDash: interacting decisions at operational scale

[Engineering article, May 21, 2026](https://careersatdoordash.com/blog/supercharging-doordash-logistics-through-causal-ml-and-joint-optimization/).
Jointly models incentives and batching across nearly half a million geography
and time units. Uses learning experiments, including multi-arm switchbacks,
and R-learners with LightGBM. It warns that observational models can reverse
the apparent effect because incentives respond to poor supply. Transferable
lesson: represent interacting exposures and distinguish selection from effects.

### Stanford, Kumo AI, SAP: preserve multi-table structure

[PluRel](https://star-project.stanford.edu/plurel/), first submitted February 3,
2026 and [revised July 14](https://arxiv.org/abs/2602.04029), ICML 2026.
Separates schema, foreign-key connectivity, and feature-generation mechanisms
when synthesizing databases for relational foundation models. Its evaluations
concern predictive learning. Synthetic causal mechanisms do not establish
real-world causal effects from NBA observations. The architectural lesson is
to preserve relationships rather than discard them during feature preparation.

### Microsoft Research: intervention-aware causal discovery

[TICL](https://www.microsoft.com/en-us/research/publication/test-time-learning-of-causal-structure-from-interventional-data/),
listed February 2026 / ICML 2026. Uses test-time learning and joint causal
inference to recover structure and intervention targets, evaluated on bnlearn
benchmarks. This supports retaining intervention information where available;
it is not evidence that box scores alone identify an injury's causal effect.

### Columbia and DTU: hierarchy matters

[Hierarchical Causal Models](https://jmlr.org/papers/volume27/25-0899/25-0899.pdf),
JMLR, January 2026. Formalizes nested units and subunits, including confounding
and some interference structures. Shows why aggregation can remove useful
identification information. Basketball has crossed relationships across games,
teams, and players, so a simple fixed nesting is not a complete NBA model.

### Airbnb: connected users change experimental design

[Collaborative User Networks draft](https://airbnb.tech/wp-content/uploads/sites/19/2026/01/Experimental_Design_with_Host_Networks_Paper_MITCODE_Draft.pdf)
uses network-level randomization and instrumental variables for voluntary
adoption. Airbnb's [year-in-review](https://airbnb.tech/infrastructure/academic-publications-airbnb-tech-2025-year-in-review/)
places it at MIT CODE 2025. The January 2026 upload path is not its publication
date; the PDF still contains template metadata. Relevant lesson: connected
participants violate independent-unit assumptions. NBA absences lack this
randomized assignment mechanism.

## Is a wide dbt context table conventional?

Yes, as a purpose-built mart with a clear grain. [dbt's current guidance](https://docs.getdbt.com/best-practices/how-we-structure/4-marts)
supports denormalized entity marts, while recommending more normalization with
its Semantic Layer. It advises introducing incremental materialization when
build costs justify the complexity. This project has its own semantic contract;
that alone does not mean it uses dbt's MetricFlow Semantic Layer.

Neither dbt nor a graph database supplies causal identification. Three graphs
have different meanings: a dbt dependency graph tracks transformations; a
relationship graph tracks entities and membership; a causal graph asserts
directional mechanisms and omitted-confounder assumptions.

## Revised NBA architecture

| Layer | Proposed records and grain | Purpose |
| --- | --- | --- |
| Governed facts | Existing player-game box scores; team-game facts | Retain counts, attempts, minutes, IDs, event time, provenance |
| Dated relationships | Player-team membership interval; timestamped player/game availability reports | Preserve trades, status revisions, report coverage, and knowledge time |
| Shared playing context, later | Stint; stint-player membership | Represent five-on-five groups without storing every pair as the primary record |
| Reusable context | Team-game pregame defense/form; player-game pregame features | Reuse prior-only computations across comparisons |
| Derived mart | `player_game_context`, one player-game | Serve statistics and bounded summaries without changing its grain |
| Study panel | `study_id`, focal player, game, exposure definition | Assemble eligible observations for a versioned causal or associative question |
| Evidence result | Study, outcome, contrast, estimator version | Store counts, estimates, intervals, diagnostics, source IDs, allowed claim level |

Keep stable typed tables rather than a universal untyped entity-attribute-value
table. Foreign keys and membership tables already allow graph projections if
later models require them. A stint is naturally a shared group event: membership
rows preserve its ten players, and pairwise relationships can be derived.

Avoid columns such as `trae_out`, `luka_out`, or one column for every lineup.
Encode the selected exposure through a player ID and versioned definition.
For other absences, derive compact prior-weighted measures, such as missing
teammates' prior minutes or creation share, while preserving the underlying
identities. These are modeling choices to validate, not sufficient adjustments
by definition.

Avoid materializing every player-by-player-by-game combination. Store team/game
membership once, then derive selected pair studies or bounded neighborhood
summaries. Never join multiple many-to-many relations into the mart without
first restoring its player-game grain: otherwise attempts and points multiply.

At first, use straightforward views or tables and measure query bytes, latency,
and build time. The inspected player-game fact currently uses table
materialization. Consider date partitions, player/team clustering, and
incremental refresh only with measured benefit. Corrections to old injury or
roster events require rebuilding affected studies and dependent feature
windows; a recent-date lookback alone is not a complete correction strategy.
Freeze source and study versions so human reviews remain reproducible.

## A defensible first study

Treat Jalen Johnson/Trae Young as a hypothesis; verify the eligible shared-roster
period and dated absence evidence before computing effects. Define what
"unavailable" means and distinguish it from simply recording zero minutes.

Hypothesized pathway:

`teammate absence -> focal minutes / creation responsibility -> assists`

Pregame health, opponent quality, rest, other absences, coaching regimes, and
calendar time can complicate the comparison. For total effects, do not blindly
adjust away actual minutes or usage: they may carry the effect being studied.
Actual on-court/off-court exposure is a separate study requiring lineup stints.

Start with observed splits, then adjusted associations if both exposure groups
have comparable games. Report games and independent absence episodes, sample
balance, overlap, and uncertainty; consider episode-level dependence. Define
how focal-player nonparticipation is handled. If effects cannot be separated
from concurrent absences or roster changes, return that limitation.

A causal estimate requires a defensible identification strategy and sensitivity
analysis. Graph learning, matching, double ML, or a fluent explanation does not
remove this requirement. Industry experiment results cannot substitute for
missing intervention evidence in this dataset.

## Reviewable next milestone

1. Enrich the existing ten frozen answers with deterministic shooting, attempt,
   minutes, and efficiency summaries; retain original answers for comparison.
2. Audit roster/injury history and opponent-stat coverage, then build the dated
   relations and one focused study panel. Show unknown coverage explicitly.
3. Review descriptive and adjusted results before considering a more complex
   causal model. Include unsupported/low-overlap cases in evaluation.

Keep SQL/Python computation outside the LLM. Send Luna a compact evidence
packet; retain Terra for offline evaluation and the user for final review.
Record warehouse bytes/time and model input/cached/output tokens separately
for each step. Record zero LLM tokens for deterministic steps but retain their
compute costs. Measure accuracy and token changes before claiming savings.
