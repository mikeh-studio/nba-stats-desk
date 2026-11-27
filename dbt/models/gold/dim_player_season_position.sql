{{ config(
    materialized='table',
    schema=env_var('BQ_DATASET_GOLD', env_var('BQ_DATASET', 'nba_gold'))
) }}

with profiles as (
    select
        player_id,
        player_name,
        replace(replace(replace(upper(trim(position)), 'GUARD', 'G'), 'FORWARD', 'F'), 'CENTER', 'C') as listed_position,
        ingested_at_utc
    from {{ ref('stg_player_reference_clean') }}
    -- CommonPlayerInfo is a current profile without a requested roster season.
    where '{{ warehouse_season() }}' = '{{ var("nba_current_season") }}'
      and date(ingested_at_utc) between date('{{ warehouse_season_start() }}') and date('{{ warehouse_season_end() }}')
      and roster_status
      and player_id > 0
    qualify row_number() over (
        partition by player_id
        order by ingested_at_utc desc, player_name, position
    ) = 1
),
classified as (
    select
        *,
        {{ listed_position_groups('listed_position') }} as position_groups
    from profiles
)
select
    '{{ warehouse_season() }}' as season,
    player_id,
    player_name,
    listed_position,
    position_groups,
    'current_player_reference' as position_source,
    concat('https://www.nba.com/player/', cast(player_id as {{ varchar_type() }})) as source_ref,
    false as is_verified_season_roster,
    ingested_at_utc
from classified
where array_length(position_groups) > 0
