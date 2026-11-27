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
    where cast(season as {{ varchar_type() }}) = '{{ warehouse_season() }}'
      and date(game_date) between date('{{ warehouse_season_start() }}') and date('{{ warehouse_season_end() }}')
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
        {% if 'pf' in ns.column_names %}
        cast(pf as {{ float64_type() }}) as pf,
        {% else %}
        cast(null as {{ float64_type() }}) as pf,
        {% endif %}
        {% if 'oreb' in ns.column_names %}
        cast(oreb as {{ float64_type() }}) as oreb,
        {% else %}
        cast(null as {{ float64_type() }}) as oreb,
        {% endif %}
        {% if 'dreb' in ns.column_names %}
        cast(dreb as {{ float64_type() }}) as dreb,
        {% else %}
        cast(null as {{ float64_type() }}) as dreb,
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
            {% if 'game_id' in ns.column_names %}
            case
                when substring(trim(cast(game_id as {{ varchar_type() }})), 1, 3) = '004' then 'Playoffs'
                when substring(cast(game_id as {{ varchar_type() }}), 1, 3) = '002' then 'Regular Season'
                else 'Regular Season'
            end
            {% else %}
            'Regular Season'
            {% endif %}
        ) as season_type,
        {% elif 'game_id' in ns.column_names %}
        case
            when substring(trim(cast(game_id as {{ varchar_type() }})), 1, 3) = '004' then 'Playoffs'
            when substring(cast(game_id as {{ varchar_type() }}), 1, 3) = '002' then 'Regular Season'
            else 'Regular Season'
        end as season_type,
        {% else %}
        'Regular Season' as season_type,
        {% endif %}
        cast(player_id as {{ int64_type() }}) as player_id,
        cast(player_name as {{ varchar_type() }}) as player_name,
        cast(ingested_at_utc as timestamp) as ingested_at_utc,
        {% for field in playoff_round_columns() if field in ns.column_names %}
        {{ field }},
        {% endfor %}
        row_number() over (
            partition by player_id, game_date, matchup
            order by ingested_at_utc desc
        ) as row_num
    from source_data
),
deduped_logs as (
    select
        *,
        {{ playoff_round_explicit_inputs(ns.column_names) }}
    from deduped
    where row_num = 1
),
schedule_rounds as (
    select
        game_id,
        case
            when count(distinct playoff_round_number) = 1
                and count(playoff_round_number) = count(*)
            then min(playoff_round_number)
        end as sched_round_number,
        case
            when count(distinct playoff_series_id) = 1
                and count(playoff_series_id) = count(*)
            then min(playoff_series_id)
        end as sched_series_id,
        case
            when count(distinct playoff_series_game_number) = 1
                and count(playoff_series_game_number) = count(*)
            then min(playoff_series_game_number)
        end as sched_series_game_number,
        (
            count(distinct playoff_round_number) > 1
            or count(distinct playoff_series_id) > 1
            or count(distinct playoff_series_game_number) > 1
            or count(playoff_round_number) != count(*)
        ) as has_sched_conflict
    from {{ ref('stg_schedule_clean') }}
    group by 1
),
log_round_inputs as (
    select
        game_id,
        case
            when count(distinct season_type) = 1
                and count(season_type) = count(*)
            then min(season_type)
        end as resolved_season_type,
        case
            when count(distinct explicit_playoff_round_number) = 1
            then min(explicit_playoff_round_number)
        end as log_round_number,
        case
            when count(distinct explicit_playoff_series_id) = 1
            then min(explicit_playoff_series_id)
        end as log_series_id,
        case
            when count(distinct explicit_playoff_series_game_number) = 1
            then min(explicit_playoff_series_game_number)
        end as log_series_game_number,
        (
            max(case when explicit_playoff_round_row_conflict then 1 else 0 end) = 1
            or count(distinct explicit_playoff_round_number) > 1
            or count(distinct explicit_playoff_series_id) > 1
            or count(distinct explicit_playoff_series_game_number) > 1
            or count(distinct season_type) > 1
        ) as has_log_conflict
    from deduped_logs
    group by 1
),
game_rounds as (
    select
        l.game_id,
        {{ playoff_round_resolved_columns(
            'l.game_id',
            'l.resolved_season_type',
            'coalesce(l.log_round_number, s.sched_round_number)',
            'coalesce(l.log_series_id, s.sched_series_id)',
            'coalesce(l.log_series_game_number, s.sched_series_game_number)',
            '(l.has_log_conflict or coalesce(s.has_sched_conflict, false))'
        ) }}
    from log_round_inputs l
    left join schedule_rounds s on l.game_id = s.game_id
)
select
    logs.game_id,
    logs.game_date,
    logs.matchup,
    logs.wl,
    logs.min,
    logs.fgm,
    logs.fga,
    logs.fg_pct,
    logs.ftm,
    logs.fta,
    logs.ft_pct,
    logs.fg3m,
    logs.fg3a,
    logs.plus_minus,
    logs.pf,
    logs.oreb,
    logs.dreb,
    logs.pts,
    logs.reb,
    logs.ast,
    logs.stl,
    logs.blk,
    logs.tov,
    logs.season,
    logs.season_type,
    logs.player_id,
    logs.player_name,
    logs.ingested_at_utc,
    {% for field in playoff_round_columns() %}
    rounds.{{ field }}{% if not loop.last %},{% endif %}
    {% endfor %}
from deduped_logs logs
left join game_rounds rounds on logs.game_id = rounds.game_id
