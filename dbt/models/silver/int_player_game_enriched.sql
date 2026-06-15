{{ config(
    materialized='table',
    schema=env_var('BQ_DATASET_SILVER', env_var('BQ_DATASET', 'nba_silver'))
) }}

select
    game_id,
    game_date,
    matchup,
    wl,
    min,
    fgm,
    fga,
    fg_pct,
    ftm,
    fta,
    ft_pct,
    fg3m,
    fg3a,
    plus_minus,
    pts,
    reb,
    ast,
    stl,
    blk,
    tov,
    season,
    season_type,
    player_id,
    player_name,
    upper({{ regex_extract('matchup', "'^([A-Z]{2,3})'") }}) as team_abbr,
    upper({{ regex_extract('matchup', "'([A-Z]{2,3})$'") }}) as opponent_abbr,
    case
        when {{ regex_contains('matchup', "'@'") }} then 'AWAY'
        when {{ regex_contains('matchup', "'vs\\\\.'") }} then 'HOME'
        else 'UNKNOWN'
    end as home_away,
    ingested_at_utc
from {{ ref('stg_game_logs_clean') }}
