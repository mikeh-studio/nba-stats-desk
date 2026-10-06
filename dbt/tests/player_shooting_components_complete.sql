-- Required box-score components must not silently publish as partial metrics.
select game_id, player_id
from {{ ref('fct_player_game_stats') }}
where fgm is null or fga is null or fg3m is null or fg3a is null
   or ftm is null or fta is null or plus_minus is null
   or fgm > fga or fg3m > fg3a or ftm > fta
   or fg3m > fgm or fg3a > fga
   or pts <> 2 * fgm + fg3m + ftm
