{{ config(
    materialized='table',
    schema=env_var('BQ_DATASET_GOLD', env_var('BQ_DATASET', 'nba_gold'))
) }}

with shot_locations as (
    select *
    from {{ ref('stg_player_shot_locations_clean') }}
    where season_type = 'Regular Season'
),
player_totals as (
    select
        season,
        player_id,
        any_value(player_name) as player_name,
        sum(coalesce(restricted_area_fgm, 0)) as restricted_area_fgm,
        sum(coalesce(restricted_area_fga, 0)) as restricted_area_fga,
        sum(coalesce(paint_non_ra_fgm, 0)) as paint_non_ra_fgm,
        sum(coalesce(paint_non_ra_fga, 0)) as paint_non_ra_fga,
        sum(coalesce(mid_range_fgm, 0)) as mid_range_fgm,
        sum(coalesce(mid_range_fga, 0)) as mid_range_fga,
        sum(coalesce(left_corner3_fgm, 0)) as left_corner3_fgm,
        sum(coalesce(left_corner3_fga, 0)) as left_corner3_fga,
        sum(coalesce(right_corner3_fgm, 0)) as right_corner3_fgm,
        sum(coalesce(right_corner3_fga, 0)) as right_corner3_fga,
        sum(coalesce(above_break3_fgm, 0)) as above_break3_fgm,
        sum(coalesce(above_break3_fga, 0)) as above_break3_fga,
        sum(coalesce(backcourt_fgm, 0)) as backcourt_fgm,
        sum(coalesce(backcourt_fga, 0)) as backcourt_fga,
        max(ingested_at_utc) as latest_shot_location_ingested_at_utc
    from shot_locations
    group by 1, 2
),
profile as (
    select
        *,
        restricted_area_fga
        + paint_non_ra_fga
        + mid_range_fga
        + left_corner3_fga
        + right_corner3_fga
        + above_break3_fga
        + backcourt_fga as shot_location_fga
    from player_totals
),
canonical_profile as (
    select p.*
    from profile p
    inner join {{ ref('dim_player') }} d
        on p.player_id = d.player_id
       and p.season = d.latest_season
)
select
    season,
    player_id,
    player_name,
    shot_location_fga,
    restricted_area_fgm,
    restricted_area_fga,
    round({{ safe_divide('restricted_area_fga', 'nullif(shot_location_fga, 0)') }}, 4) as shot_rim_rate,
    round({{ safe_divide('restricted_area_fgm', 'nullif(restricted_area_fga, 0)') }}, 4) as shot_rim_fg_pct,
    paint_non_ra_fgm,
    paint_non_ra_fga,
    round({{ safe_divide('paint_non_ra_fga', 'nullif(shot_location_fga, 0)') }}, 4) as shot_paint_non_ra_rate,
    round({{ safe_divide('paint_non_ra_fgm', 'nullif(paint_non_ra_fga, 0)') }}, 4) as shot_paint_non_ra_fg_pct,
    mid_range_fgm,
    mid_range_fga,
    round({{ safe_divide('mid_range_fga', 'nullif(shot_location_fga, 0)') }}, 4) as shot_midrange_rate,
    round({{ safe_divide('mid_range_fgm', 'nullif(mid_range_fga, 0)') }}, 4) as shot_midrange_fg_pct,
    left_corner3_fgm,
    left_corner3_fga,
    right_corner3_fgm,
    right_corner3_fga,
    left_corner3_fgm + right_corner3_fgm as corner3_fgm,
    left_corner3_fga + right_corner3_fga as corner3_fga,
    round(
        {{ safe_divide(
            'left_corner3_fga + right_corner3_fga',
            'nullif(shot_location_fga, 0)'
        ) }},
        4
    ) as shot_corner3_rate,
    round(
        {{ safe_divide(
            'left_corner3_fgm + right_corner3_fgm',
            'nullif(left_corner3_fga + right_corner3_fga, 0)'
        ) }},
        4
    ) as shot_corner3_fg_pct,
    above_break3_fgm,
    above_break3_fga,
    round({{ safe_divide('above_break3_fga', 'nullif(shot_location_fga, 0)') }}, 4) as shot_above_break3_rate,
    round({{ safe_divide('above_break3_fgm', 'nullif(above_break3_fga, 0)') }}, 4) as shot_above_break3_fg_pct,
    backcourt_fgm,
    backcourt_fga,
    round({{ safe_divide('backcourt_fga', 'nullif(shot_location_fga, 0)') }}, 4) as shot_backcourt_rate,
    latest_shot_location_ingested_at_utc
from canonical_profile
