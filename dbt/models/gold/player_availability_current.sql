{{ config(
    materialized='table',
    schema=env_var('BQ_DATASET_GOLD', env_var('BQ_DATASET', 'nba_gold'))
) }}

{% set today = 'current_date()' if target.type == 'bigquery' else 'current_date' %}
{% set stale_cutoff = 'date_sub(current_date(), interval 7 day)' if target.type == 'bigquery' else "current_date - interval '7 day'" %}

with injury_rows as (
    select *
    from {{ ref('stg_player_injury_reports_clean') }}
    where player_id is not null
),
latest_injury as (
    select
        *,
        row_number() over (
            partition by season, player_id
            order by report_timestamp_utc desc, game_date desc, ingested_at_utc desc
        ) as row_num
    from injury_rows
),
current_injury as (
    select *
    from latest_injury
    where row_num = 1
)
select
    {{ today }} as as_of_date,
    i.season,
    i.player_id,
    coalesce(i.player_name, p.player_name) as player_name,
    coalesce(i.team_abbr, p.latest_team_abbr) as team_abbr,
    i.injury_status,
    case
        when i.injury_status = 'Out' then 'unavailable'
        when i.injury_status in ('Doubtful', 'Questionable') then 'at_risk'
        when i.injury_status = 'Probable' then 'probable'
        when i.injury_status = 'Available' then 'available'
        else 'unknown'
    end as availability_bucket,
    case
        when i.injury_status = 'Out' then 10.0
        when i.injury_status = 'Doubtful' then 8.0
        when i.injury_status = 'Questionable' then 5.0
        when i.injury_status = 'Probable' then 2.0
        when i.injury_status = 'Available' then 0.0
        else 4.0
    end as availability_risk_score,
    i.reason,
    i.report_date,
    case when i.report_date < {{ stale_cutoff }} then true else false end as is_report_stale,
    i.report_time_et,
    i.report_timestamp_utc,
    i.game_date as next_reported_game_date,
    i.game_time_et as next_reported_game_time_et,
    i.matchup as next_reported_matchup,
    o.first_game_date,
    o.next_opponent,
    o.next_7d_games,
    o.next_7d_back_to_backs,
    i.source_system,
    i.source_url,
    i.ingested_at_utc
from current_injury i
inner join {{ ref('dim_player') }} p
    on i.player_id = p.player_id
left join {{ ref('player_opportunity_outlook') }} o
    on i.player_id = o.player_id
   and i.season = o.season
