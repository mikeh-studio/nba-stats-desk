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
with source_rows as (
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
        cast(source_updated_at_utc as timestamp) as source_updated_at_utc,
        {{ "coalesce(nullif(trim(cast(season_type as " ~ varchar_type() ~ ")), ''), 'Playoffs')" if 'season_type' in schedule_columns else "'Playoffs'" }} as round_season_type,
        {{ playoff_round_explicit_inputs(schedule_columns) }}
    from {{ source('bronze', 'raw_schedule') }}
    where cast(schedule_date as date) between date('{{ warehouse_season_start() }}') and date('{{ warehouse_season_end() }}')
),
game_round_inputs as (
    select
        game_id,
        case
            when count(distinct round_season_type) = 1
                and count(round_season_type) = count(*)
            then min(round_season_type)
        end as round_season_type,
        case
            when count(distinct explicit_playoff_round_number) = 1
            then min(explicit_playoff_round_number)
        end as explicit_playoff_round_number,
        case
            when count(distinct explicit_playoff_series_id) = 1
            then min(explicit_playoff_series_id)
        end as explicit_playoff_series_id,
        case
            when count(distinct explicit_playoff_series_game_number) = 1
            then min(explicit_playoff_series_game_number)
        end as explicit_playoff_series_game_number,
        (
            max(case when explicit_playoff_round_row_conflict then 1 else 0 end) = 1
            or count(distinct explicit_playoff_round_number) > 1
            or count(distinct explicit_playoff_series_id) > 1
            or count(distinct explicit_playoff_series_game_number) > 1
            or count(distinct round_season_type) > 1
        ) as has_round_conflict
    from source_rows
    group by 1
),
game_rounds as (
    select
        game_id,
        explicit_playoff_round_number,
        explicit_playoff_series_id,
        explicit_playoff_series_game_number,
        has_round_conflict as playoff_round_conflict,
        {{ playoff_round_resolved_columns(
            'game_id',
            'round_season_type',
            'explicit_playoff_round_number',
            'explicit_playoff_series_id',
            'explicit_playoff_series_game_number',
            'has_round_conflict'
        ) }}
    from game_round_inputs
)
select
    s.game_id,
    s.schedule_date,
    s.team_abbr,
    s.opponent_abbr,
    s.home_away,
    s.is_back_to_back,
    s.game_status,
    s.scheduled_start_utc,
    s.ingested_at_utc,
    s.source_updated_at_utc,
    r.explicit_playoff_round_number,
    r.explicit_playoff_series_id,
    r.explicit_playoff_series_game_number,
    r.playoff_round_conflict,
    {% for field in playoff_round_columns() %}
    r.{{ field }}{% if not loop.last %},{% endif %}
    {% endfor %}
from source_rows s
left join game_rounds r on s.game_id = r.game_id
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
    cast(null as timestamp) as source_updated_at_utc,
    cast(null as {{ int64_type() }}) as explicit_playoff_round_number,
    cast(null as {{ varchar_type() }}) as explicit_playoff_series_id,
    cast(null as {{ int64_type() }}) as explicit_playoff_series_game_number,
    cast(null as {{ bool_type() }}) as playoff_round_conflict,
    {{ playoff_round_fields("cast(null as " ~ varchar_type() ~ ")", "'Regular Season'") }}
from (select 1 as _empty_source)
where 1 = 0
{% endif %}
