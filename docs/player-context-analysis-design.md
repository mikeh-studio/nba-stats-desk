# Player context and teammate effects: proposed next milestone

Status: first descriptive slice implemented and locally/read-only warehouse
validated on 2026-09-16. No causal model or production deployment. See
[implementation and validation](player-context-milestone.md). Full historical
membership, latest-before-tipoff availability, and adjusted studies remain future work.

Architecture clarification (2026-09-16): `player_game_context` is a derived
serving/analysis mart, not the canonical store for every relationship. Preserve
dated membership and availability facts independently; derive focused exposure
panels for individual studies. See [recent research and revised architecture](relational-causal-research-2026.md).

## Repository findings

- `app/agent/performance_overview.py` restricts the overview to points, rebounds,
  assists, steals, and blocks. The reporting evaluation inherits that restriction.
- `app/agent/semantic_contract.yml` already defines minutes, shooting percentages,
  turnovers, and TS%. The frozen evaluation snapshots retain makes and attempts.
- `dbt/models/gold/fct_player_game_stats.sql` additionally contains home/away,
  offensive/defensive rebounds, fouls, and ingestion time. Snapshot capture does
  not currently retain all those fields.
- `what_changed_injury_reports` preserves historical report timestamps and
  source URLs. `player_availability_current` is a current-state view and must not
  be used to label historical availability.
- `fct_team_game_scores` has scores and opponents, not possession-based defense.
- Shot-location input is a player/team/season aggregate. It cannot establish a
  monthly shot-mix change. Historical shot-level or dated aggregates are needed.
- Inspected contracts/models do not provide lineup stints, potential assists,
  touches, or historical roster membership intervals. Source coverage and access
  must be verified before treating these as available.

## Model grains and relationships

Use BigQuery/dbt relations with stable player/team/game IDs. Preserve season and
phase in contracts. Names are display fields. No graph database is required.

| Proposed relation | Grain | Important fields |
| --- | --- | --- |
| `player_game_context` (derived mart) | season, game, player | governed box counts and minutes joined from facts; team/opponent IDs, venue, rest, own pregame status, opponent-context ID; distinguish pregame inputs from outcomes |
| `player_team_membership` | player, team, effective interval | valid_from/to, known_at, source, confidence; trades and roster moves |
| `player_game_availability` | game, rostered player | last pre-tipoff report/time, status, reason, report coverage, actual participation, actual minutes, DNP reason |
| `team_defense_before_game` | game, opponent team | prior-only defensive rating, pace, opponent eFG%, turnover/rebound measures, source games/possessions, method/version |
| `player_teammate_game` (focused projection) | game, focal player, teammate | derive selected pairs from membership and availability; shared-roster flag, pregame status, actual participation, prior minutes/creation shares, evidence IDs; avoid universal pair expansion |
| `lineup_stint` and `lineup_stint_player` (later) | uninterrupted lineup interval; interval, player | start/end, five players per team, elapsed seconds, possessions, score state at entry, events, reconstruction quality |
| `player_context_comparison` | study, focal player, exposure, outcome | cohort rules, both samples, raw/adjusted difference, uncertainty, balance/overlap, estimator version, evidence and claim level |

Every source-backed fact needs event time, publication/availability time when
known, ingestion time, and source identity. Distinguish retrospectively corrected
facts from information actually captured before tipoff. An article containing
multiple players also needs subject/relationship tags so a team transaction is
not misread as evidence that a particular teammate's role changed.

Build historical eligible roster membership first, then left-join appearances
and reports. A missing box-score row is not proof of injury. Keep unknown report
coverage separate from available, injured, resting, coach DNP, and not rostered.
Do not classify Trae's post-trade games as Atlanta injury absences. Do not infer
an absence reason, surgery, or recovery without dated supporting evidence.

Game-level co-participation is not shared court time. True on/off comparisons
require validated stints/substitutions. Keep those concepts separately named.

## First extension: richer deterministic statistics

For each window, expose games, total/average minutes, FGA/3PA/FTA per game,
FG%/3P%/FT%, eFG%, TS%, turnovers, and points/assists per 36 minutes. Show makes
and attempts with rates. Use ratios of aggregate counts, not unweighted averages
of game percentages; preserve null denominators and incomplete components.
Shooting-rate changes are percentage points. Per-36 uses total production over
total minutes; it describes production rate, not causal effectiveness.

Reuse the governed TS% approximation; add eFG% and attempt metrics to the
contract. Do not present either shooting measure as complete player efficiency.
Extend unit-aware generation, reference rendering, and independent auditing:
the current step audit assumes each metric is a simple per-game mean.

Opponent defensive rating should use points allowed per 100 possessions, not
points allowed per game. Prefer verified team possessions. A box-score-derived
possession estimate needs a documented formula and team-total coverage checks.
Compute prior-only season-to-date and rolling windows, shrink small samples,
and expose coverage. Do not use a final-season rank to explain an earlier game.
Team defensive quality does not identify the defender guarding a player.

## Teammate and opponent comparisons

Start with prespecified Jalen Johnson/Trae Young and LeBron James/Luka Doncic
pairings, verifying roster overlap and actual absences. The user's examples are
hypotheses, not findings established by this design. Separate injury periods
from trades and coach/rotation changes.

Report games and minutes in both groups, assists and attempts per game, per-36
rates, turnovers, shooting efficiency, opponent quality, and other absences.
Choose important teammates using prior-period minutes or creation contribution,
not whichever split produces the largest effect. Team assist share is an
allocation measure, not proof of primary playmaking. Potential assists, touches,
and time on ball would more directly test creation responsibility if acquired.
Assists also depend on recipients converting shots.

Then estimate adjusted associations within comparable team/season/role periods,
accounting for pregame opponent strength, venue, rest, own prior form/health,
other relevant absences, and calendar time. Check balance and common support.
If every relevant absence coincides with another injury or a regime change,
report that the individual effect cannot be separated. Repeated games within
an absence episode are correlated; uncertainty must reflect that clustering.

## Causal interpretation boundary

Specify the comparison before modeling: among eligible shared-roster games,
what would the focal player's total assists be if a teammate were available
versus unavailable under a specified absence definition? State whose games
qualify and how focal-player nonparticipation is handled. Conditioning on focal
participation changes the target and can introduce selection bias.

Hypothesized pathway: teammate absence -> minutes/ball handling/shot allocation
-> focal-player outcomes. Opponent strength, schedule, coaching decisions,
health, and other absences can influence both exposure and outcome. Actual
minutes, usage, same-game pace, and final margin can be downstream of exposure;
do not blindly adjust for them when estimating the total effect. Per-minute
associations are a separate descriptive question; direct effects need stronger
mediation assumptions and a different design.

Matching or regression alone does not prove causality. Any later causal study
needs a precise exposure, appropriate time zero, adequate overlap, defensible
confounder measurement, an explicit interference/exposure model for teammates,
and sensitivity checks. An event study needs credible comparison groups and
pre-trend checks; no automatic difference-in-differences claim after a trade.
SHAP feature attribution is not causal effect estimation.

Persist claim levels: `descriptive`, `adjusted_association`, and
`causal_estimate_under_assumptions`. Insufficient coverage/overlap prevents
promotion to stronger claims. News can support the proposed mechanism, but does
not identify an effect size. An unknown result must remain reportable.

## Bounded implementation order and review gates

1. Enrich the same 10 frozen 2025-26 cases with existing shooting/minutes fields.
   Independently verify ratio aggregation, units, rounding, and denominators.
2. Audit historical injury/roster and opponent coverage; implement the four core
   context relations and focused teammate bridge. Unknowns must remain unknown.
3. Add the two prespecified pair studies with observed splits, then adjusted
   associations if sample overlap permits. Include negative/null-result cases.
4. Source and validate stints/tracking only after the game-level study is useful.

Calculate comparisons in SQL/Python. Pass the generator compact metric rows,
the relevant teammate contrasts, opponent summary, sample sizes, uncertainty,
evidence IDs, and allowed claim level. Keep full rows behind the review UI.
Target a measured per-answer evidence budget; do not promise savings before
recording input/output tokens. Keep Terra evaluation offline.

Review gates: season/game/player uniqueness; historical roster boundaries;
no absent-to-injured inference; latest valid pregame report selection; no future
opponent data; complete team totals for derived possessions; ratio and missing
denominator correctness; small-sample/overlap suppression; original vs enriched
answer comparison; and token/cost reporting. Add deterministic checks for derived
counts such as 9 minus 7, which Terra missed in run-02.

## References

- [NBA statistical definitions](https://www.nba.com/stats/help/glossary)
- [Hernan et al., target trial framework](https://pubmed.ncbi.nlm.nih.gov/39961105/)
- [CAUSALab methods and observational study design](https://hsph.harvard.edu/research/causalab/what-we-do/)
