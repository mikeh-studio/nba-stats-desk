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
),
per_game_uniqueness as (
    select
        model,
        game_id
    from scoped_games
    group by 1, 2
    having count(distinct season_type) > 1
        or count(distinct coalesce(cast(playoff_round_number as {{ varchar_type() }}), '<null>')) > 1
        or count(distinct coalesce(playoff_round_name, '<null>')) > 1
        or count(distinct coalesce(playoff_series_id, '<null>')) > 1
        or count(distinct coalesce(cast(playoff_series_game_number as {{ varchar_type() }}), '<null>')) > 1
        or count(distinct coalesce(playoff_round_source, '<null>')) > 1
),
schedule_uniqueness as (
    select
        'stg_schedule_clean' as model,
        game_id
    from {{ ref('stg_schedule_clean') }}
    group by 1, 2
    having count(distinct coalesce(cast(playoff_round_number as {{ varchar_type() }}), '<null>')) > 1
        or count(distinct coalesce(playoff_round_name, '<null>')) > 1
        or count(distinct coalesce(playoff_series_id, '<null>')) > 1
        or count(distinct coalesce(cast(playoff_series_game_number as {{ varchar_type() }}), '<null>')) > 1
        or count(distinct coalesce(playoff_round_source, '<null>')) > 1
),
gold_mismatch as (
    select
        'dim_game_vs_fct_player_game_stats' as model,
        f.game_id
    from {{ ref('fct_player_game_stats') }} f
    full outer join {{ ref('dim_game') }} d
        on f.game_id = d.game_id
    where f.season_type is distinct from d.season_type
        or f.playoff_round_number is distinct from d.playoff_round_number
        or f.playoff_round_name is distinct from d.playoff_round_name
        or f.playoff_series_id is distinct from d.playoff_series_id
        or f.playoff_series_game_number is distinct from d.playoff_series_game_number
        or f.playoff_round_source is distinct from d.playoff_round_source
),
schedule_conflicts as (
    select distinct
        'stg_schedule_clean_conflict' as model,
        game_id
    from {{ ref('stg_schedule_clean') }}
    where coalesce(playoff_round_conflict, false)
        or playoff_round_source = 'conflict'
),
log_games as (
    select distinct
        game_id,
        season_type,
        playoff_round_number,
        playoff_series_id,
        playoff_series_game_number,
        playoff_round_source
    from {{ ref('stg_game_logs_clean') }}
    where season_type = 'Playoffs'
),
schedule_games as (
    select distinct
        game_id,
        explicit_playoff_round_number,
        explicit_playoff_series_id,
        explicit_playoff_series_game_number,
        playoff_round_conflict,
        playoff_round_source
    from {{ ref('stg_schedule_clean') }}
),
schedule_vs_logs_mismatch as (
    select
        'stg_schedule_vs_stg_game_logs_clean' as model,
        l.game_id
    from log_games l
    inner join schedule_games s
        on l.game_id = s.game_id
    where coalesce(s.playoff_round_conflict, false)
        or s.playoff_round_source = 'conflict'
        or l.playoff_round_source = 'conflict'
        or (
            s.explicit_playoff_round_number is not null
            and l.playoff_round_number is distinct from s.explicit_playoff_round_number
        )
        or (
            s.explicit_playoff_series_id is not null
            and l.playoff_series_id is distinct from s.explicit_playoff_series_id
        )
        or (
            s.explicit_playoff_series_game_number is not null
            and l.playoff_series_game_number is distinct from s.explicit_playoff_series_game_number
        )
)
select model, game_id
from scoped_games
where (
    (season_type is distinct from 'Playoffs'
        or not {{ regex_contains('game_id', "'^004[0-9]{2}00[1-4][0-9][1-7]$'") }})
    and (
        playoff_round_number is not null
        or playoff_round_name is not null
        or playoff_series_id is not null
        or playoff_series_game_number is not null
        or playoff_round_source is distinct from 'none'
    )
)
or (
    season_type = 'Playoffs'
    and {{ regex_contains('game_id', "'^004[0-9]{2}00[1-4][0-9][1-7]$'") }}
    and (
        playoff_round_number is null
        or playoff_round_number not between 1 and 4
        or playoff_round_name is distinct from {{ playoff_round_name('playoff_round_number') }}
        or playoff_series_id is null
        or playoff_series_game_number is null
        or playoff_series_game_number not between 1 and 7
        or playoff_round_source not in ('explicit', 'game_id')
    )
)
union all
select model, game_id from per_game_uniqueness
union all
select model, game_id from schedule_uniqueness
union all
select model, game_id from gold_mismatch
union all
select model, game_id from schedule_conflicts
union all
select model, game_id from schedule_vs_logs_mismatch
