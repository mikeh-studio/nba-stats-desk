{{ config(materialized='view', schema=env_var('BQ_DATASET_GOLD', env_var('BQ_DATASET', 'nba_gold'))) }}

-- Derived player-game mart. Relationship records retain their own grain.
select p.*,
    d.prior_games as opponent_prior_games,
    d.valid_prior_games as opponent_valid_prior_games,
    d.valid_shooting_prior_games as opponent_valid_shooting_prior_games,
    d.latest_prior_game_date as opponent_latest_prior_game_date,
    case when d.valid_prior_games >= 5 and d.valid_prior_games = d.prior_games
        then d.prior_win_pct else null end as opponent_prior_win_pct,
    case when d.valid_shooting_prior_games >= 5 and d.valid_shooting_prior_games = d.prior_games
        then d.prior_efg_allowed_pct else null end as opponent_prior_efg_allowed_pct,
    coalesce(a.injury_status, 'Unknown') as prior_day_reported_status,
    a.report_timestamp_utc as status_reported_at,
    a.ingested_at_utc as status_ingested_at,
    a.source_url as status_source_url,
    'before_game_date_utc' as status_cutoff_policy,
    'descriptive' as claim_level
from {{ ref('fct_player_game_stats') }} p
left join {{ ref('team_defense_before_game') }} d
    on p.season = d.season and p.season_type = d.season_type
    and p.game_id = d.game_id and p.opponent_abbr = d.team_abbr
left join {{ ref('player_game_reported_status') }} a
    on p.season = a.season and p.game_id = a.game_id
    and p.player_id = a.player_id and p.team_abbr = a.team_abbr
