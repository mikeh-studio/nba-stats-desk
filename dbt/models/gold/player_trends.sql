{{ config(
    materialized='view',
    schema=env_var('BQ_DATASET_GOLD', env_var('BQ_DATASET', 'nba_gold'))
) }}

with numbered as (
    select
        season,
        player_id,
        player_name,
        min,
        pts,
        reb,
        ast,
        stl,
        blk,
        tov,
        fg_pct,
        ft_pct,
        fg3m,
        fantasy_points_simple,
        row_number() over (
            partition by player_id
            order by game_date desc
        ) as game_num
    from {{ ref('fct_player_game_stats') }}
    where coalesce(cast(min as {{ float64_type() }}), 0) >= 1
),
base as (
    select * from numbered where game_num <= 10
),
pts_trend as (
    select
        max(season) as season,
        player_id,
        player_name,
        'PTS' as stat,
        {{ countif('game_num <= 5') }} as recent_games,
        {{ countif('game_num between 6 and 10') }} as prior_games,
        round(avg(case when game_num <= 5 then pts end), 1) as recent_avg,
        round(avg(case when game_num between 6 and 10 then pts end), 1) as prior_avg
    from base
    group by 2, 3
),
reb_trend as (
    select
        max(season) as season,
        player_id,
        player_name,
        'REB' as stat,
        {{ countif('game_num <= 5') }} as recent_games,
        {{ countif('game_num between 6 and 10') }} as prior_games,
        round(avg(case when game_num <= 5 then reb end), 1) as recent_avg,
        round(avg(case when game_num between 6 and 10 then reb end), 1) as prior_avg
    from base
    group by 2, 3
),
ast_trend as (
    select
        max(season) as season,
        player_id,
        player_name,
        'AST' as stat,
        {{ countif('game_num <= 5') }} as recent_games,
        {{ countif('game_num between 6 and 10') }} as prior_games,
        round(avg(case when game_num <= 5 then ast end), 1) as recent_avg,
        round(avg(case when game_num between 6 and 10 then ast end), 1) as prior_avg
    from base
    group by 2, 3
),
stl_trend as (
    select
        max(season) as season,
        player_id,
        player_name,
        'STL' as stat,
        {{ countif('game_num <= 5') }} as recent_games,
        {{ countif('game_num between 6 and 10') }} as prior_games,
        round(avg(case when game_num <= 5 then stl end), 1) as recent_avg,
        round(avg(case when game_num between 6 and 10 then stl end), 1) as prior_avg
    from base
    group by 2, 3
),
blk_trend as (
    select
        max(season) as season,
        player_id,
        player_name,
        'BLK' as stat,
        {{ countif('game_num <= 5') }} as recent_games,
        {{ countif('game_num between 6 and 10') }} as prior_games,
        round(avg(case when game_num <= 5 then blk end), 1) as recent_avg,
        round(avg(case when game_num between 6 and 10 then blk end), 1) as prior_avg
    from base
    group by 2, 3
),
tov_trend as (
    select
        max(season) as season,
        player_id,
        player_name,
        'TOV' as stat,
        {{ countif('game_num <= 5') }} as recent_games,
        {{ countif('game_num between 6 and 10') }} as prior_games,
        round(avg(case when game_num <= 5 then tov end), 1) as recent_avg,
        round(avg(case when game_num between 6 and 10 then tov end), 1) as prior_avg
    from base
    group by 2, 3
),
min_trend as (
    select
        max(season) as season,
        player_id,
        player_name,
        'MIN' as stat,
        {{ countif('game_num <= 5') }} as recent_games,
        {{ countif('game_num between 6 and 10') }} as prior_games,
        round(avg(case when game_num <= 5 then min end), 1) as recent_avg,
        round(avg(case when game_num between 6 and 10 then min end), 1) as prior_avg
    from base
    group by 2, 3
),
fg_pct_trend as (
    select
        max(season) as season,
        player_id,
        player_name,
        'FG%' as stat,
        {{ countif('game_num <= 5') }} as recent_games,
        {{ countif('game_num between 6 and 10') }} as prior_games,
        round(avg(case when game_num <= 5 then fg_pct end) * 100, 1) as recent_avg,
        round(avg(case when game_num between 6 and 10 then fg_pct end) * 100, 1) as prior_avg
    from base
    group by 2, 3
),
ft_pct_trend as (
    select
        max(season) as season,
        player_id,
        player_name,
        'FT%' as stat,
        {{ countif('game_num <= 5') }} as recent_games,
        {{ countif('game_num between 6 and 10') }} as prior_games,
        round(avg(case when game_num <= 5 then ft_pct end) * 100, 1) as recent_avg,
        round(avg(case when game_num between 6 and 10 then ft_pct end) * 100, 1) as prior_avg
    from base
    group by 2, 3
),
fg3m_trend as (
    select
        max(season) as season,
        player_id,
        player_name,
        '3PM' as stat,
        {{ countif('game_num <= 5') }} as recent_games,
        {{ countif('game_num between 6 and 10') }} as prior_games,
        round(avg(case when game_num <= 5 then fg3m end), 1) as recent_avg,
        round(avg(case when game_num between 6 and 10 then fg3m end), 1) as prior_avg
    from base
    group by 2, 3
),
fantasy_points_trend as (
    select
        max(season) as season,
        player_id,
        player_name,
        'FANTASY_POINTS_SIMPLE' as stat,
        {{ countif('game_num <= 5') }} as recent_games,
        {{ countif('game_num between 6 and 10') }} as prior_games,
        round(avg(case when game_num <= 5 then fantasy_points_simple end), 1) as recent_avg,
        round(avg(case when game_num between 6 and 10 then fantasy_points_simple end), 1) as prior_avg
    from base
    group by 2, 3
),
unioned as (
    select * from pts_trend
    union all
    select * from reb_trend
    union all
    select * from ast_trend
    union all
    select * from stl_trend
    union all
    select * from blk_trend
    union all
    select * from tov_trend
    union all
    select * from min_trend
    union all
    select * from fg_pct_trend
    union all
    select * from ft_pct_trend
    union all
    select * from fg3m_trend
    union all
    select * from fantasy_points_trend
)
select
    season,
    player_id,
    player_name,
    stat,
    recent_games,
    prior_games,
    recent_avg,
    prior_avg,
    round(recent_avg - prior_avg, 1) as delta,
    round({{ safe_divide('recent_avg - prior_avg', 'nullif(prior_avg, 0)') }} * 100, 1) as pct_change
from unioned
where recent_games >= 3 and prior_games >= 3
