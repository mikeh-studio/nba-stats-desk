select season, game_id, player_id
from {{ ref('player_game_context') }}
where opponent_latest_prior_game_date >= game_date
    or status_reported_at >= {{ context_day_start('game_date') }}
