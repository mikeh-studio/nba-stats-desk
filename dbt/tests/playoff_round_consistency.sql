with scoped_games as (
    {% for model in ['stg_game_logs_clean', 'dim_game', 'fct_player_game_stats'] %}
    select
        '{{ model }}' as model,
        game_id,
        season_type,
        {{ playoff_round_columns() | join(', ') }}
    from {{ ref(model) }}
    {% if not loop.last %}union all{% endif %}
    {% endfor %}
)
select *
from scoped_games
where (
    (season_type is distinct from 'Playoffs'
        or not {{ regex_contains('game_id', "'^004[0-9]{2}00[1-4][0-9][1-7]$'") }})
    and (
        playoff_round_number is not null
        or playoff_round_name is not null
        or playoff_series_id is not null
        or playoff_series_game_number is not null
    )
)
or (
    playoff_round_number is not null
    and (
        playoff_round_number not between 1 and 4
        or playoff_round_name is distinct from {{ playoff_round_name('playoff_round_number') }}
        or playoff_series_game_number is null
        or playoff_series_game_number not between 1 and 7
    )
)
