with positions as (
    select
        *,
        count(*) over (partition by season, player_id) as grain_count,
        {{ listed_position_groups('listed_position') }} as expected_groups
    from {{ ref('dim_player_season_position') }}
)
select *
from positions
where grain_count != 1
   or season is distinct from '{{ warehouse_season() }}'
   or player_id is null
   or array_length(expected_groups) = 0
   or to_json_string(position_groups) is distinct from to_json_string(expected_groups)
