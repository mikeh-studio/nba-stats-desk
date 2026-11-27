{{ config(
    materialized='table',
    schema=env_var('BQ_DATASET_GOLD', env_var('BQ_DATASET', 'nba_gold'))
) }}

with game_playoff_scope as (
    select
        game_id,
        season_type,
        {% for field in playoff_round_columns() %}
        {{ field }}{% if not loop.last %},{% endif %}
        {% endfor %}
    from {{ ref('stg_game_logs_clean') }}
    qualify row_number() over (
        partition by game_id
        order by ingested_at_utc desc, player_id
    ) = 1
),
team_scores as (
    select
        t.game_id,
        t.game_date,
        t.season,
        t.team_id,
        t.team_abbr,
        t.team_pts,
        coalesce(s.home_away, t.home_away) as home_away,
        t.ingested_at_utc
    from {{ ref('fct_team_game_scores') }} t
    left join {{ ref('stg_schedule_clean') }} s
        on t.game_id = s.game_id
       and t.team_abbr = s.team_abbr
),
game_rollup as (
    select
        game_id,
        max(game_date) as game_date,
        max(season) as season,
        max(case when home_away = 'HOME' then team_abbr end) as home_team_abbr,
        max(case when home_away = 'AWAY' then team_abbr end) as away_team_abbr,
        max(case when home_away = 'HOME' then team_pts end) as home_team_pts,
        max(case when home_away = 'AWAY' then team_pts end) as away_team_pts,
        max(ingested_at_utc) as ingested_at_utc
    from team_scores
    group by 1
),
games_with_team_ids as (
    select
        g.game_id,
        g.game_date,
        g.season,
        home_team.team_id as home_team_id,
        g.home_team_abbr,
        away_team.team_id as away_team_id,
        g.away_team_abbr,
        g.home_team_pts,
        g.away_team_pts,
        g.ingested_at_utc
    from game_rollup g
    left join {{ ref('dim_team') }} home_team
        on g.home_team_abbr = home_team.team_abbr
    left join {{ ref('dim_team') }} away_team
        on g.away_team_abbr = away_team.team_abbr
)
select
    g.game_id,
    g.game_date,
    g.season,
    p.season_type,
    {% for field in playoff_round_columns() %}
    p.{{ field }},
    {% endfor %}
    home_team_id,
    home_team_abbr,
    away_team_id,
    away_team_abbr,
    case
        when home_team_pts > away_team_pts then home_team_abbr
        when away_team_pts > home_team_pts then away_team_abbr
        else null
    end as winning_team_abbr,
    home_team_pts,
    away_team_pts,
    cast(null as {{ bool_type() }}) as is_overtime,
    cast(null as {{ int64_type() }}) as overtime_periods,
    coalesce(home_team_pts, 0) + coalesce(away_team_pts, 0) as total_points,
    case
        when home_team_pts >= away_team_pts then home_team_pts - away_team_pts
        else away_team_pts - home_team_pts
    end as scoring_margin,
    ingested_at_utc
from games_with_team_ids g
left join game_playoff_scope p on g.game_id = p.game_id
where home_team_abbr is not null
  and away_team_abbr is not null
