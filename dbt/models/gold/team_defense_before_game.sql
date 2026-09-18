{{ config(materialized='view', schema=env_var('BQ_DATASET_GOLD', env_var('BQ_DATASET', 'nba_gold'))) }}

-- Strictly earlier dates; never use the current game's or final season's result.
-- Keep season phases separate. No possession-based defensive rating is inferred.
select g.season, g.season_type, g.game_id, g.game_date, g.team_abbr,
    count(p.game_id) as prior_games,
    sum(case when p.comparison_coverage_ok = 1 then 1 else 0 end) as valid_prior_games,
    sum(case when p.defense_shooting_coverage_ok = 1 then 1 else 0 end) as valid_shooting_prior_games,
    max(p.game_date) as latest_prior_game_date,
    100.0 * sum(case when p.comparison_coverage_ok = 1 then p.won end)
        / nullif(sum(case when p.comparison_coverage_ok = 1 then 1 else 0 end), 0)
        as prior_win_pct,
    100.0 * sum(case when p.defense_shooting_coverage_ok = 1 then p.opponent_fgm + 0.5 * p.opponent_fg3m end)
        / nullif(sum(case when p.defense_shooting_coverage_ok = 1 then p.opponent_fga end), 0)
        as prior_efg_allowed_pct
from {{ ref('team_game_context') }} g
left join {{ ref('team_game_context') }} p on g.season = p.season
    and g.season_type = p.season_type and g.team_abbr = p.team_abbr
    and p.game_date < g.game_date
group by g.season, g.season_type, g.game_id, g.game_date, g.team_abbr
