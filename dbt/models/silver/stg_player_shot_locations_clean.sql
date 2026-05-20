{{ config(
    materialized='table',
    schema=env_var('BQ_DATASET_SILVER', env_var('BQ_DATASET', 'nba_silver'))
) }}

{% set raw_relation = source('bronze', 'raw_player_shot_locations') %}
{% set existing_relation = raw_relation %}
{% if execute and raw_relation.database == 'local-project' %}
  {% set existing_relation = none %}
{% elif execute %}
  {% set existing_relation = adapter.get_relation(
      database=raw_relation.database,
      schema=raw_relation.schema,
      identifier=raw_relation.identifier
  ) %}
{% endif %}

with source_data as (
    {% if execute and existing_relation is none %}
    select
        cast(null as {{ int64_type() }}) as player_id,
        cast(null as {{ varchar_type() }}) as player_name,
        cast(null as {{ int64_type() }}) as team_id,
        cast(null as {{ varchar_type() }}) as team_abbr,
        cast(null as {{ float64_type() }}) as age,
        cast(null as {{ varchar_type() }}) as season,
        cast(null as {{ varchar_type() }}) as season_type,
        cast(null as {{ float64_type() }}) as restricted_area_fgm,
        cast(null as {{ float64_type() }}) as restricted_area_fga,
        cast(null as {{ float64_type() }}) as restricted_area_fg_pct,
        cast(null as {{ float64_type() }}) as paint_non_ra_fgm,
        cast(null as {{ float64_type() }}) as paint_non_ra_fga,
        cast(null as {{ float64_type() }}) as paint_non_ra_fg_pct,
        cast(null as {{ float64_type() }}) as mid_range_fgm,
        cast(null as {{ float64_type() }}) as mid_range_fga,
        cast(null as {{ float64_type() }}) as mid_range_fg_pct,
        cast(null as {{ float64_type() }}) as left_corner3_fgm,
        cast(null as {{ float64_type() }}) as left_corner3_fga,
        cast(null as {{ float64_type() }}) as left_corner3_fg_pct,
        cast(null as {{ float64_type() }}) as right_corner3_fgm,
        cast(null as {{ float64_type() }}) as right_corner3_fga,
        cast(null as {{ float64_type() }}) as right_corner3_fg_pct,
        cast(null as {{ float64_type() }}) as above_break3_fgm,
        cast(null as {{ float64_type() }}) as above_break3_fga,
        cast(null as {{ float64_type() }}) as above_break3_fg_pct,
        cast(null as {{ float64_type() }}) as backcourt_fgm,
        cast(null as {{ float64_type() }}) as backcourt_fga,
        cast(null as {{ float64_type() }}) as backcourt_fg_pct,
        cast(null as timestamp) as ingested_at_utc
    where false
    {% else %}
    select
        cast(player_id as {{ int64_type() }}) as player_id,
        cast(player_name as {{ varchar_type() }}) as player_name,
        cast(team_id as {{ int64_type() }}) as team_id,
        upper(cast(team_abbr as {{ varchar_type() }})) as team_abbr,
        cast(age as {{ float64_type() }}) as age,
        cast(season as {{ varchar_type() }}) as season,
        cast(season_type as {{ varchar_type() }}) as season_type,
        cast(restricted_area_fgm as {{ float64_type() }}) as restricted_area_fgm,
        cast(restricted_area_fga as {{ float64_type() }}) as restricted_area_fga,
        cast(restricted_area_fg_pct as {{ float64_type() }}) as restricted_area_fg_pct,
        cast(paint_non_ra_fgm as {{ float64_type() }}) as paint_non_ra_fgm,
        cast(paint_non_ra_fga as {{ float64_type() }}) as paint_non_ra_fga,
        cast(paint_non_ra_fg_pct as {{ float64_type() }}) as paint_non_ra_fg_pct,
        cast(mid_range_fgm as {{ float64_type() }}) as mid_range_fgm,
        cast(mid_range_fga as {{ float64_type() }}) as mid_range_fga,
        cast(mid_range_fg_pct as {{ float64_type() }}) as mid_range_fg_pct,
        cast(left_corner3_fgm as {{ float64_type() }}) as left_corner3_fgm,
        cast(left_corner3_fga as {{ float64_type() }}) as left_corner3_fga,
        cast(left_corner3_fg_pct as {{ float64_type() }}) as left_corner3_fg_pct,
        cast(right_corner3_fgm as {{ float64_type() }}) as right_corner3_fgm,
        cast(right_corner3_fga as {{ float64_type() }}) as right_corner3_fga,
        cast(right_corner3_fg_pct as {{ float64_type() }}) as right_corner3_fg_pct,
        cast(above_break3_fgm as {{ float64_type() }}) as above_break3_fgm,
        cast(above_break3_fga as {{ float64_type() }}) as above_break3_fga,
        cast(above_break3_fg_pct as {{ float64_type() }}) as above_break3_fg_pct,
        cast(backcourt_fgm as {{ float64_type() }}) as backcourt_fgm,
        cast(backcourt_fga as {{ float64_type() }}) as backcourt_fga,
        cast(backcourt_fg_pct as {{ float64_type() }}) as backcourt_fg_pct,
        cast(ingested_at_utc as timestamp) as ingested_at_utc
    from {{ existing_relation }}
    where cast(season as {{ varchar_type() }}) = '2025-26'
    {% endif %}
),
deduped as (
    select
        *,
        row_number() over (
            partition by season, season_type, player_id, team_id
            order by ingested_at_utc desc
        ) as row_num
    from source_data
    where player_id is not null
      and team_id is not null
      and season is not null
      and season_type is not null
)
select
    player_id,
    player_name,
    team_id,
    team_abbr,
    age,
    season,
    season_type,
    restricted_area_fgm,
    restricted_area_fga,
    restricted_area_fg_pct,
    paint_non_ra_fgm,
    paint_non_ra_fga,
    paint_non_ra_fg_pct,
    mid_range_fgm,
    mid_range_fga,
    mid_range_fg_pct,
    left_corner3_fgm,
    left_corner3_fga,
    left_corner3_fg_pct,
    right_corner3_fgm,
    right_corner3_fga,
    right_corner3_fg_pct,
    above_break3_fgm,
    above_break3_fga,
    above_break3_fg_pct,
    backcourt_fgm,
    backcourt_fga,
    backcourt_fg_pct,
    ingested_at_utc
from deduped
where row_num = 1
