# Trending

The **Trending** page (`/what-changed`) supports basketball analysis and fantasy research through
league-wide Top Performers and Surging lists. It describes observed box scores;
it does not predict future performance or implement league-specific scoring.

## Comparison contract

- Default: each team's latest four games versus its previous four, anchored to
  the latest published game in the selected season phase. Missing appearances
  remain visible in availability counts, rather than extending the window.
- Alternative: latest complete Monday–Sunday week versus the preceding week,
  relative to that source date. A partial week is never compared with a full week.
- Regular season and playoffs are separate. An optional through date limits
  game dates. It is not a historical warehouse snapshot: later source corrections
  to earlier games can still appear.
- Player cards show exact dates. During the offseason, completed-season results
  remain dated historical comparisons. No games are synthesized.
- Team assignment uses the player's last observed team. Teams inactive for more
  than six days relative to the comparison anchor are excluded. Recorded game
  logs cannot prove roster membership on days without a player appearance.

## Ranking contract

The existing project production proxy is `PTS + REB + AST + STL + BLK - TOV`
per recorded appearance. Top Performers sorts recent production; Surging sorts
positive recent-minus-prior changes. Ties resolve by player ID.

Four-game rankings require a complete four-team-game window, at least three
appearances, and at least ten minutes per appearance. Surging also needs a
complete prior window with three appearances and excludes team changes across
the comparison. Weekly rankings require two recent appearances; Surging also
requires two prior appearances. Players without sufficient baselines can rank
as top performers but not as surging players.

Availability expands into minutes, personal fouls, appearances, missing
appearances, and dated injury-report coverage. Offense expands into points,
assists, threes, offensive rebounds, turnovers, FG%, FT%, and TS%. Defense
expands into defensive rebounds, steals, and blocks. Counting stats include
per-appearance values and totals, allowing fantasy researchers to inspect volume.

Percentages use total makes and attempts, not averages of game percentages;
changes are percentage points. TS% is `100 * PTS / (2 * (FGA + 0.44 * FTA))`,
following the [NBA glossary](https://www.nba.com/stats/help/glossary).
Zero attempts and missing fields remain unknown. Missing appearances never
become zero-stat games in per-appearance averages.

## Evidence and limitations

Every card includes underlying games. Injury evidence uses dated reports,
matched to player, team, and game date; reports dated after the game or requested
cutoff are excluded. An Out report is a reported status, not a confirmed absence.
Missing reports cannot establish health. Current ingestion omits non-playing
roster entries, so DNP-CD is explicitly unavailable and is never inferred.

Defense measures box-score events, not total defensive impact. Four games are a
small descriptive sample. The production proxy is not ESPN/Yahoo scoring, an
all-in-one basketball rating, or a personalized fantasy recommendation.

## Data rollout and serving

The API reads a bounded 60-day slice of `fct_player_game_stats` (maximum 20,000
rows). Exceeding the cap or failing the core query returns 503 instead of a
partial league ranking. Missing historical injury data remains optional.

The dbt changes forward raw PF, OREB, and DREB through the silver models into
the gold fact table. Existing deployed tables remain readable, with these new
fields shown as unknown until the normal core dbt build publishes them. Raw
columns absent from a source schema also remain null.

`what_changed_injury_reports` publishes through the existing injury candidate
branch and atomic promotion transaction. Run the normal pipeline to build and
validate these assets; this feature's local validation does not deploy or rebuild
the live warehouse. Historical injury coverage depends on reports previously
collected and does not imply a full-season backfill.

The API caches successful comparisons for 15 minutes, keyed by repository,
period, phase, and cutoff. Browser QA uses explicitly fictional fixture players;
it verifies interaction and layout, not live warehouse completeness.

## Sports Journal presentation

The shared UI uses locally hosted Barlow and Barlow Condensed, the original
charcoal/orange palette, square controls, and underlined navigation. The opportunity
map plots minutes change against production change per appearance. Axis ranges
adapt to the loaded data and include zero; positive and negative changes remain
visible. Only the selected point is labeled to avoid a wall of overlapping names.

A player requires a sufficient prior window and no team change to appear on the
map. Top performers without a comparable baseline remain in the ranked list.
The list provides individual selection for overlapping points and the map supports
keyboard selection. Player details retain all metric clusters, totals, injury
coverage, and individual game evidence.
