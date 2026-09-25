{{ config(
    materialized='view',
    schema=env_var('BQ_DATASET_SILVER', env_var('BQ_DATASET', 'nba_silver'))
) }}

{% set schedule_relation = adapter.get_relation(
    database=env_var('BQ_PROJECT', env_var('GCP_PROJECT_ID', 'local-project')),
    schema=env_var('BQ_DATASET_BRONZE', env_var('BQ_DATASET', 'nba_bronze')),
    identifier='raw_schedule'
) %}

{% if schedule_relation is not none %}
{% set schedule_columns = adapter.get_columns_in_relation(schedule_relation) | map(attribute='name') | map('lower') | list %}
select
    cast(game_id as {{ varchar_type() }}) as game_id,
    cast(schedule_date as date) as schedule_date,
    upper(cast(team_abbr as {{ varchar_type() }})) as team_abbr,
    upper(cast(opponent_abbr as {{ varchar_type() }})) as opponent_abbr,
    cast(home_away as {{ varchar_type() }}) as home_away,
    cast(coalesce(is_back_to_back, false) as {{ bool_type() }}) as is_back_to_back,
    cast(
        case
            when game_status is null or trim(cast(game_status as {{ varchar_type() }})) = '' then 'scheduled'
            when lower(trim(cast(game_status as {{ varchar_type() }}))) in ('scheduled', 'pre-game', 'tbd') then 'scheduled'
            when regexp_contains(lower(trim(cast(game_status as {{ varchar_type() }}))), r'^[0-9]{1,2}:[0-9]{2}\s*(am|pm)\s*et$') then 'scheduled'
            when lower(trim(cast(game_status as {{ varchar_type() }}))) in ('final', 'final/ot', 'bootstrapped_from_game_logs') then 'final'
            when lower(trim(cast(game_status as {{ varchar_type() }}))) like 'final%' then 'final'
            when lower(trim(cast(game_status as {{ varchar_type() }}))) in ('postponed', 'ppd') then 'postponed'
            else lower(trim(cast(game_status as {{ varchar_type() }})))
        end as {{ varchar_type() }}
    ) as game_status,
    cast({{ 'game_time_utc' if 'game_time_utc' in schedule_columns else 'null' }} as timestamp) as scheduled_start_utc,
    cast({{ 'ingested_at_utc' if 'ingested_at_utc' in schedule_columns else 'null' }} as timestamp) as ingested_at_utc,
    cast(source_updated_at_utc as timestamp) as source_updated_at_utc
from {{ source('bronze', 'raw_schedule') }}
where cast(schedule_date as date) between date('{{ warehouse_season_start() }}') and date('{{ warehouse_season_end() }}')
{% else %}
select
    cast(null as {{ varchar_type() }}) as game_id,
    cast(null as date) as schedule_date,
    cast(null as {{ varchar_type() }}) as team_abbr,
    cast(null as {{ varchar_type() }}) as opponent_abbr,
    cast(null as {{ varchar_type() }}) as home_away,
    cast(null as {{ bool_type() }}) as is_back_to_back,
    cast(null as {{ varchar_type() }}) as game_status,
    cast(null as timestamp) as scheduled_start_utc,
    cast(null as timestamp) as ingested_at_utc,
    cast(null as timestamp) as source_updated_at_utc
from (select 1 as _empty_source)
where 1 = 0
{% endif %}
