select season, game_id, player_id
from {{ ref('player_game_context') }}
group by season, game_id, player_id
having count(*) != 1
