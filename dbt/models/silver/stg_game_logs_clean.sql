{{ config(
    materialized='table',
    schema=env_var('BQ_DATASET_SILVER', env_var('BQ_DATASET', 'nba_silver'))
) }}

{% set raw_game_logs_relation = source('bronze', 'raw_game_logs') %}
{% set raw_game_logs_relation = get_required_relation(
    raw_game_logs_relation,
    [
        'game_date',
        'matchup',
        'season',
        'player_id',
        'player_name',
        'pts',
        'reb',
        'ast',
        'stl',
        'blk',
        'tov',
        'ingested_at_utc',
    ]
) %}
{% set raw_game_logs_columns = adapter.get_columns_in_relation(raw_game_logs_relation) %}
{% set ns = namespace(column_names=[]) %}
{% for column in raw_game_logs_columns %}
  {% set ns.column_names = ns.column_names + [column.name | lower] %}
{% endfor %}
{% set derived_game_id_expression %}
concat(
    cast(date(game_date) as {{ varchar_type() }}),
    '_',
    least(
        upper({{ regex_extract('matchup', "'^([A-Z]{2,3})'") }}),
        upper({{ regex_extract('matchup', "'([A-Z]{2,3})$'") }})
    ),
    '_',
    greatest(
        upper({{ regex_extract('matchup', "'^([A-Z]{2,3})'") }}),
        upper({{ regex_extract('matchup', "'([A-Z]{2,3})$'") }})
    )
)
{% endset %}

with source_data as (
    select *
    from {{ raw_game_logs_relation }}
    where cast(season as {{ varchar_type() }}) = '2025-26'
      and date(game_date) between date('2025-07-01') and date('2026-06-30')
),
deduped as (
    select
        {% if 'game_id' in ns.column_names %}
        coalesce(
            nullif(trim(cast(game_id as {{ varchar_type() }})), ''),
            {{ derived_game_id_expression }}
        ) as game_id,
        {% else %}
        {{ derived_game_id_expression }} as game_id,
        {% endif %}
        date(game_date) as game_date,
        cast(matchup as {{ varchar_type() }}) as matchup,
        upper(cast(wl as {{ varchar_type() }})) as wl,
        cast(min as {{ float64_type() }}) as min,
        {% if 'fgm' in ns.column_names %}
        cast(fgm as {{ float64_type() }}) as fgm,
        {% else %}
        cast(null as {{ float64_type() }}) as fgm,
        {% endif %}
        {% if 'fga' in ns.column_names %}
        cast(fga as {{ float64_type() }}) as fga,
        {% else %}
        cast(null as {{ float64_type() }}) as fga,
        {% endif %}
        {% if 'fg_pct' in ns.column_names %}
        cast(fg_pct as {{ float64_type() }}) as fg_pct,
        {% else %}
        cast(null as {{ float64_type() }}) as fg_pct,
        {% endif %}
        {% if 'ftm' in ns.column_names %}
        cast(ftm as {{ float64_type() }}) as ftm,
        {% else %}
        cast(null as {{ float64_type() }}) as ftm,
        {% endif %}
        {% if 'fta' in ns.column_names %}
        cast(fta as {{ float64_type() }}) as fta,
        {% else %}
        cast(null as {{ float64_type() }}) as fta,
        {% endif %}
        {% if 'ft_pct' in ns.column_names %}
        cast(ft_pct as {{ float64_type() }}) as ft_pct,
        {% else %}
        cast(null as {{ float64_type() }}) as ft_pct,
        {% endif %}
        {% if 'fg3m' in ns.column_names %}
        cast(fg3m as {{ float64_type() }}) as fg3m,
        {% else %}
        cast(null as {{ float64_type() }}) as fg3m,
        {% endif %}
        {% if 'fg3a' in ns.column_names %}
        cast(fg3a as {{ float64_type() }}) as fg3a,
        {% else %}
        cast(null as {{ float64_type() }}) as fg3a,
        {% endif %}
        {% if 'plus_minus' in ns.column_names %}
        cast(plus_minus as {{ float64_type() }}) as plus_minus,
        {% else %}
        cast(null as {{ float64_type() }}) as plus_minus,
        {% endif %}
        cast(pts as {{ int64_type() }}) as pts,
        cast(reb as {{ int64_type() }}) as reb,
        cast(ast as {{ int64_type() }}) as ast,
        cast(stl as {{ int64_type() }}) as stl,
        cast(blk as {{ int64_type() }}) as blk,
        cast(tov as {{ int64_type() }}) as tov,
        cast(season as {{ varchar_type() }}) as season,
        {% if 'season_type' in ns.column_names %}
        coalesce(
            nullif(trim(cast(season_type as {{ varchar_type() }})), ''),
            case
                when substring(cast(game_id as {{ varchar_type() }}), 1, 3) = '004' then 'Playoffs'
                when substring(cast(game_id as {{ varchar_type() }}), 1, 3) = '002' then 'Regular Season'
                else 'Regular Season'
            end
        ) as season_type,
        {% elif 'game_id' in ns.column_names %}
        case
            when substring(cast(game_id as {{ varchar_type() }}), 1, 3) = '004' then 'Playoffs'
            when substring(cast(game_id as {{ varchar_type() }}), 1, 3) = '002' then 'Regular Season'
            else 'Regular Season'
        end as season_type,
        {% else %}
        'Regular Season' as season_type,
        {% endif %}
        cast(player_id as {{ int64_type() }}) as player_id,
        cast(player_name as {{ varchar_type() }}) as player_name,
        cast(ingested_at_utc as timestamp) as ingested_at_utc,
        row_number() over (
            partition by player_id, game_date, matchup
            order by ingested_at_utc desc
        ) as row_num
    from source_data
)
select
    game_id,
    game_date,
    matchup,
    wl,
    min,
    fgm,
    fga,
    fg_pct,
    ftm,
    fta,
    ft_pct,
    fg3m,
    fg3a,
    plus_minus,
    pts,
    reb,
    ast,
    stl,
    blk,
    tov,
    season,
    season_type,
    player_id,
    player_name,
    ingested_at_utc
from deduped
where row_num = 1
