{{ config(
    materialized='table',
    schema=env_var('BQ_DATASET_GOLD', env_var('BQ_DATASET', 'nba_gold'))
) }}

{% set today = 'current_date()' if target.type == 'bigquery' else 'current_date' %}

with scored_games as (
    select
        season,
        player_id,
        player_name,
        team_abbr,
        game_date,
        season_type,
        min,
        pts,
        reb,
        ast,
        stl,
        blk,
        fg3m,
        tov,
        round(
            pts
            + (reb * 1.2)
            + (ast * 1.5)
            + (stl * 3.0)
            + (blk * 3.0)
            + fg3m
            - tov,
            2
        ) as fantasy_proxy_score,
        row_number() over (
            partition by season, player_id
            order by game_date desc
        ) as game_num,
        row_number() over (
            partition by season, player_id, season_type
            order by game_date asc
        ) as season_type_game_num,
        count(*) over (
            partition by season, player_id, season_type
        ) as season_type_games
    from {{ ref('fct_player_game_stats') }}
),
season_stats as (
    select
        season,
        player_id,
        max(game_date) as latest_game_date,
        count(*) as season_games,
        round(avg(min), 1) as season_avg_min,
        round(avg(pts), 1) as season_avg_pts,
        round(avg(reb), 1) as season_avg_reb,
        round(avg(ast), 1) as season_avg_ast,
        round(avg(stl), 1) as season_avg_stl,
        round(avg(blk), 1) as season_avg_blk,
        round(avg(fg3m), 1) as season_avg_fg3m,
        round(avg(tov), 1) as season_avg_tov,
        round(avg(fantasy_proxy_score), 1) as season_avg_fantasy_proxy
    from scored_games
    group by 1, 2
),
latest_player as (
    select
        season,
        player_id,
        player_name,
        team_abbr as latest_team_abbr
    from scored_games
    where game_num = 1
),
windowed as (
    select
        season,
        player_id,
        'last_3' as window_key,
        3 as window_games_expected,
        min,
        pts,
        reb,
        ast,
        stl,
        blk,
        fg3m,
        tov,
        fantasy_proxy_score
    from scored_games
    where game_num <= 3

    union all

    select
        season,
        player_id,
        'last_5' as window_key,
        5 as window_games_expected,
        min,
        pts,
        reb,
        ast,
        stl,
        blk,
        fg3m,
        tov,
        fantasy_proxy_score
    from scored_games
    where game_num <= 5

    union all

    select
        season,
        player_id,
        'last_7' as window_key,
        7 as window_games_expected,
        min,
        pts,
        reb,
        ast,
        stl,
        blk,
        fg3m,
        tov,
        fantasy_proxy_score
    from scored_games
    where game_num <= 7

    union all

    select
        season,
        player_id,
        'prior_5' as window_key,
        5 as window_games_expected,
        min,
        pts,
        reb,
        ast,
        stl,
        blk,
        fg3m,
        tov,
        fantasy_proxy_score
    from scored_games
    where game_num between 6 and 10

    union all

    select
        season,
        player_id,
        'last_10' as window_key,
        10 as window_games_expected,
        min,
        pts,
        reb,
        ast,
        stl,
        blk,
        fg3m,
        tov,
        fantasy_proxy_score
    from scored_games
    where game_num <= 10

    union all

    select
        season,
        player_id,
        'regular_season' as window_key,
        cast(null as {{ int64_type() }}) as window_games_expected,
        min,
        pts,
        reb,
        ast,
        stl,
        blk,
        fg3m,
        tov,
        fantasy_proxy_score
    from scored_games
    where season_type = 'Regular Season'

    union all

    select
        season,
        player_id,
        'regular_season_first_half' as window_key,
        cast(null as {{ int64_type() }}) as window_games_expected,
        min,
        pts,
        reb,
        ast,
        stl,
        blk,
        fg3m,
        tov,
        fantasy_proxy_score
    from scored_games
    where season_type = 'Regular Season'
      and season_type_game_num <= cast(ceil(season_type_games / 2.0) as {{ int64_type() }})

    union all

    select
        season,
        player_id,
        'regular_season_second_half' as window_key,
        cast(null as {{ int64_type() }}) as window_games_expected,
        min,
        pts,
        reb,
        ast,
        stl,
        blk,
        fg3m,
        tov,
        fantasy_proxy_score
    from scored_games
    where season_type = 'Regular Season'
      and season_type_game_num > cast(ceil(season_type_games / 2.0) as {{ int64_type() }})

    union all

    select
        season,
        player_id,
        'playoffs' as window_key,
        cast(null as {{ int64_type() }}) as window_games_expected,
        min,
        pts,
        reb,
        ast,
        stl,
        blk,
        fg3m,
        tov,
        fantasy_proxy_score
    from scored_games
    where season_type = 'Playoffs'
),
aggregated as (
    select
        season,
        player_id,
        window_key,
        window_games_expected,
        count(*) as games_in_window,
        round(avg(min), 1) as avg_min,
        round(avg(pts), 1) as avg_pts,
        round(avg(reb), 1) as avg_reb,
        round(avg(ast), 1) as avg_ast,
        round(avg(stl), 1) as avg_stl,
        round(avg(blk), 1) as avg_blk,
        round(avg(fg3m), 1) as avg_fg3m,
        round(avg(tov), 1) as avg_tov,
        round(avg(fantasy_proxy_score), 1) as fantasy_proxy_score
    from windowed
    group by 1, 2, 3, 4
)
select
    {{ today }} as as_of_date,
    a.season,
    a.player_id,
    p.player_name,
    p.latest_team_abbr,
    s.latest_game_date,
    s.season_games,
    a.window_key,
    a.window_games_expected,
    a.games_in_window,
    cast(
        case
            when a.window_games_expected is null then true
            else a.games_in_window = a.window_games_expected
        end as {{ bool_type() }}
    ) as has_full_window,
    a.avg_min,
    a.avg_pts,
    a.avg_reb,
    a.avg_ast,
    a.avg_stl,
    a.avg_blk,
    a.avg_fg3m,
    a.avg_tov,
    a.fantasy_proxy_score,
    s.season_avg_min,
    s.season_avg_pts,
    s.season_avg_reb,
    s.season_avg_ast,
    s.season_avg_stl,
    s.season_avg_blk,
    s.season_avg_fg3m,
    s.season_avg_tov,
    s.season_avg_fantasy_proxy
from aggregated a
inner join season_stats s
    on a.season = s.season
   and a.player_id = s.player_id
inner join latest_player p
    on a.season = p.season
   and a.player_id = p.player_id
