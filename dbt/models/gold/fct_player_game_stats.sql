{{ config(
    materialized='table',
    schema=env_var('BQ_DATASET_GOLD', env_var('BQ_DATASET', 'nba_gold'))
) }}

select
    game_id,
    player_id,
    player_name,
    team_abbr,
    opponent_abbr,
    home_away,
    game_date,
    matchup,
    season,
    season_type,
    wl,
    min,
    cast(fgm as {{ int64_type() }}) as fgm,
    cast(fga as {{ int64_type() }}) as fga,
    cast(fg_pct as {{ float64_type() }}) as fg_pct,
    cast(ftm as {{ int64_type() }}) as ftm,
    cast(fta as {{ int64_type() }}) as fta,
    cast(ft_pct as {{ float64_type() }}) as ft_pct,
    cast(fg3m as {{ int64_type() }}) as fg3m,
    cast(fg3a as {{ int64_type() }}) as fg3a,
    cast(plus_minus as {{ int64_type() }}) as plus_minus,
    pts,
    reb,
    ast,
    stl,
    blk,
    tov,
    round(pts + reb + ast + stl + blk - tov, 1) as fantasy_points_simple,
    ingested_at_utc
from {{ ref('int_player_game_enriched') }}
