{{ config(
    materialized='table',
    schema=env_var('BQ_DATASET_GOLD', env_var('BQ_DATASET', 'nba_gold'))
) }}

with line_scores as (
    select *
    from {{ ref('stg_game_line_scores_clean') }}
),
player_team_context as (
    select
        game_id,
        max(game_date) as game_date,
        max(season) as season,
        team_abbr,
        max(opponent_abbr) as opponent_team_abbr,
        case when count(pts) = count(*) then sum(pts) else null end as player_team_pts,
        case
            when max(case when home_away = 'HOME' then 1 else 0 end) = 1 then 'HOME'
            when max(case when home_away = 'AWAY' then 1 else 0 end) = 1 then 'AWAY'
            else null
        end as player_home_away,
        max(ingested_at_utc) as ingested_at_utc
    from {{ ref('int_player_game_enriched') }}
    group by game_id, team_abbr
),
line_team_scores as (
    select
        l.game_id,
        l.game_date,
        l.season,
        l.team_id,
        l.team_abbr,
        l.team_city_name,
        l.team_nickname,
        l.team_wins_losses,
        p.player_home_away as home_away,
        l.pts_qtr1,
        l.pts_qtr2,
        l.pts_qtr3,
        l.pts_qtr4,
        l.pts_ot1,
        l.pts_ot2,
        l.pts_ot3,
        l.pts_ot4,
        l.pts_ot5,
        l.pts_ot6,
        l.pts_ot7,
        l.pts_ot8,
        l.pts_ot9,
        l.pts_ot10,
        case
            when coalesce(l.pts, 0) = 0 and coalesce(p.player_team_pts, 0) > 0
                then p.player_team_pts
            else l.pts
        end as team_pts,
        case when l.pts_ot1 is null and l.pts_ot2 is null
            and l.pts_ot3 is null and l.pts_ot4 is null and l.pts_ot5 is null
            and l.pts_ot6 is null and l.pts_ot7 is null and l.pts_ot8 is null
            and l.pts_ot9 is null and l.pts_ot10 is null then null
        else (
            coalesce(l.pts_ot1, 0)
            + coalesce(l.pts_ot2, 0)
            + coalesce(l.pts_ot3, 0)
            + coalesce(l.pts_ot4, 0)
            + coalesce(l.pts_ot5, 0)
            + coalesce(l.pts_ot6, 0)
            + coalesce(l.pts_ot7, 0)
            + coalesce(l.pts_ot8, 0)
            + coalesce(l.pts_ot9, 0)
            + coalesce(l.pts_ot10, 0)
        ) end as team_pts_ot_total,
        l.ingested_at_utc
    from line_scores l
    inner join player_team_context p
        on l.game_id = p.game_id
       and l.team_abbr = p.team_abbr
),
player_only_team_scores as (
    select
        p.game_id,
        p.game_date,
        p.season,
        d.team_id,
        p.team_abbr,
        d.team_city_name,
        d.team_nickname,
        cast(null as {{ varchar_type() }}) as team_wins_losses,
        p.player_home_away as home_away,
        cast(null as {{ int64_type() }}) as pts_qtr1,
        cast(null as {{ int64_type() }}) as pts_qtr2,
        cast(null as {{ int64_type() }}) as pts_qtr3,
        cast(null as {{ int64_type() }}) as pts_qtr4,
        cast(null as {{ int64_type() }}) as pts_ot1,
        cast(null as {{ int64_type() }}) as pts_ot2,
        cast(null as {{ int64_type() }}) as pts_ot3,
        cast(null as {{ int64_type() }}) as pts_ot4,
        cast(null as {{ int64_type() }}) as pts_ot5,
        cast(null as {{ int64_type() }}) as pts_ot6,
        cast(null as {{ int64_type() }}) as pts_ot7,
        cast(null as {{ int64_type() }}) as pts_ot8,
        cast(null as {{ int64_type() }}) as pts_ot9,
        cast(null as {{ int64_type() }}) as pts_ot10,
        p.player_team_pts as team_pts,
        cast(null as {{ int64_type() }}) as team_pts_ot_total,
        p.ingested_at_utc
    from player_team_context p
    left join line_scores l
        on p.game_id = l.game_id
       and p.team_abbr = l.team_abbr
    left join {{ ref('dim_team') }} d
        on p.team_abbr = d.team_abbr
    where l.game_id is null
),
team_scores as (
    select * from line_team_scores
    union all
    select * from player_only_team_scores
),
with_opponent as (
    select
        t.game_id,
        t.game_date,
        t.season,
        t.team_id,
        t.team_abbr,
        t.team_city_name,
        t.team_nickname,
        t.team_wins_losses,
        t.home_away,
        o.team_id as opponent_team_id,
        o.team_abbr as opponent_team_abbr,
        o.team_pts as opponent_team_pts,
        t.pts_qtr1 as team_pts_qtr1,
        t.pts_qtr2 as team_pts_qtr2,
        t.pts_qtr3 as team_pts_qtr3,
        t.pts_qtr4 as team_pts_qtr4,
        t.team_pts_ot_total,
        t.team_pts,
        t.team_pts - o.team_pts as scoring_margin,
        case
            when t.team_pts > o.team_pts then 'W'
            when t.team_pts < o.team_pts then 'L'
            else 'T'
        end as game_result,
        t.ingested_at_utc
    from team_scores t
    left join team_scores o
        on t.game_id = o.game_id
       and t.team_id != o.team_id
)
select *
from with_opponent
