{% if target.type == 'bigquery' %}

{{ config(severity='warn') }}

with injury_rows as (
    select
        season,
        game_date,
        player_id,
        player_name,
        team_abbr,
        matchup,
        injury_status,
        regexp_extract(matchup, r'^([A-Z]{2,3})@') as away_team,
        regexp_extract(matchup, r'@([A-Z]{2,3})$') as home_team,
        report_date,
        report_time_et,
        report_timestamp_utc,
        game_time_et,
        reason,
        safe_cast(regexp_extract(game_time_et, r'^(\d{1,2}):') as int64) as game_hour_raw,
        safe_cast(regexp_extract(game_time_et, r'^\d{1,2}:(\d{2})') as int64) as game_minute
    from {{ ref('stg_player_injury_reports_clean') }}
    where season = '2025-26'
      and player_id is not null
),

injury_with_tipoff as (
    select
        *,
        timestamp(
            datetime(
                game_date,
                time(
                    case
                        when game_hour_raw = 12 then 12
                        when game_hour_raw between 1 and 11 then game_hour_raw + 12
                        else game_hour_raw
                    end,
                    coalesce(game_minute, 0),
                    0
                )
            ),
            'America/New_York'
        ) as game_start_utc
    from injury_rows
    where game_hour_raw is not null
),

latest_pregame_injury as (
    select *
    from injury_with_tipoff
    where report_timestamp_utc < game_start_utc
    qualify row_number() over (
        partition by season, game_date, player_id, matchup
        order by report_timestamp_utc desc
    ) = 1
),

played_rows as (
    select
        season,
        game_date,
        player_id,
        player_name,
        team_abbr,
        opponent_abbr,
        matchup,
        game_id,
        min
    from {{ ref('fct_player_game_stats') }}
    where season = '2025-26'
      and coalesce(min, 0) > 0
)

select
    i.game_date,
    i.report_date,
    i.report_time_et,
    i.report_timestamp_utc,
    i.game_start_utc,
    i.player_id,
    coalesce(i.player_name, p.player_name) as player_name,
    i.team_abbr as injury_team_abbr,
    p.team_abbr as played_team_abbr,
    p.opponent_abbr as played_opponent_abbr,
    i.matchup as injury_matchup,
    p.matchup as played_matchup,
    p.game_id,
    p.min as minutes_played,
    i.reason
from latest_pregame_injury i
join played_rows p
  on i.season = p.season
 and i.game_date = p.game_date
 and i.player_id = p.player_id
 and p.team_abbr in (i.away_team, i.home_team)
 and p.opponent_abbr in (i.away_team, i.home_team)
where i.injury_status = 'Out'

{% else %}

select
    cast(null as date) as game_date,
    cast(null as {{ int64_type() }}) as player_id,
    cast(null as {{ varchar_type() }}) as player_name,
    cast(null as {{ float64_type() }}) as minutes_played
from (select 1 as _empty_source)
where 1 = 0

{% endif %}
