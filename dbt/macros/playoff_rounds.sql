{% macro playoff_round_columns() %}
    {{ return([
        'playoff_round_number',
        'playoff_round_name',
        'playoff_series_id',
        'playoff_series_game_number',
        'playoff_round_source'
    ]) }}
{% endmacro %}

{% macro playoff_round_labels() %}
    {{ return(['First Round', 'Conference Semifinals', 'Conference Finals', 'NBA Finals']) }}
{% endmacro %}

{% macro canonical_playoff_game_id(game_id) -%}
    {{ regex_contains(game_id, "'^004[0-9]{2}00[1-4][0-9][1-7]$'") }}
{%- endmacro %}

{% macro playoff_round_name(round_number) -%}
    case {{ round_number }}
        {% for label in playoff_round_labels() %}
        when {{ loop.index }} then '{{ label }}'
        {% endfor %}
    end
{%- endmacro %}

{% macro playoff_round_number_from_name(round_name) -%}
    case replace(lower({{ round_name }}), '_', ' ')
        {% for label in playoff_round_labels() %}
        when '{{ label | lower }}' then {{ loop.index }}
        {% endfor %}
    end
{%- endmacro %}

{% macro playoff_round_fields(game_id, season_type, source_columns=[], source_prefix='', schedule_prefix='') -%}
    {% set fields = [
        'playoff_round_number',
        'playoff_round_name',
        'playoff_series_id',
        'playoff_series_game_number'
    ] %}
    {% set explicit = {} %}
    {% for field in fields %}
        {% set column = source_prefix ~ field %}
        {% if field not in source_columns %}
            {% do explicit.update({field: 'null'}) %}
        {% elif field in ('playoff_round_number', 'playoff_series_game_number') %}
            {% do explicit.update({field: 'cast(' ~ column ~ ' as ' ~ int64_type() ~ ')'}) %}
        {% else %}
            {% do explicit.update({field: "nullif(trim(cast(" ~ column ~ " as " ~ varchar_type() ~ ")), '')"}) %}
        {% endif %}
    {% endfor %}
    {% set round_inputs = [] %}
    {% if 'playoff_round_number' in source_columns %}
        {% set number = explicit.playoff_round_number %}
        {% do round_inputs.append('case when ' ~ number ~ ' between 1 and 4 then ' ~ number ~ ' end') %}
    {% endif %}
    {% if 'playoff_round_name' in source_columns %}
        {% do round_inputs.append(playoff_round_number_from_name(explicit.playoff_round_name)) %}
    {% endif %}
    {% if schedule_prefix %}
        {% do round_inputs.append(schedule_prefix ~ 'playoff_round_number') %}
    {% endif %}
    {% set explicit_round_expr = 'coalesce(' ~ (round_inputs | join(', ')) ~ ')' if round_inputs | length > 1 else (round_inputs[0] if round_inputs else 'null') %}
    {% do round_inputs.append('cast(substr(' ~ game_id ~ ', 8, 1) as ' ~ int64_type() ~ ')') %}
    {% set round_number = 'coalesce(' ~ (round_inputs | join(', ')) ~ ')' if round_inputs | length > 1 else round_inputs[0] %}
    {% set defaults = [
        round_number,
        playoff_round_name(round_number),
        'substr(' ~ game_id ~ ', 1, 9)',
        'cast(substr(' ~ game_id ~ ', 10, 1) as ' ~ int64_type() ~ ')'
    ] %}
    {% for field in fields %}
    case when {{ season_type }} = 'Playoffs'
        and {{ regex_contains(game_id, "'^004[0-9]{2}00[1-4][0-9][1-7]$'") }}
        then
        {% if field in ('playoff_round_number', 'playoff_round_name') %}
            {{ defaults[loop.index0] }}
        {% else %}
            coalesce(
                {{ explicit[field] }},
                {% if schedule_prefix %}
                {{ schedule_prefix }}{{ field }},
                {% endif %}
                {{ defaults[loop.index0] }}
            )
        {% endif %}
    end as {{ field }},
    {% endfor %}
    case
        when {{ season_type }} is distinct from 'Playoffs' then 'none'
        when not {{ regex_contains(game_id, "'^004[0-9]{2}00[1-4][0-9][1-7]$'") }} then 'none'
        when ({{ explicit_round_expr }}) is not null then 'explicit'
        else 'game_id'
    end as playoff_round_source
{%- endmacro %}

{% macro playoff_round_explicit_inputs(source_columns=[], source_prefix='') -%}
    {% set round_number_expr = (
        'case when cast(' ~ source_prefix ~ 'playoff_round_number as ' ~ int64_type() ~ ') between 1 and 4 '
        'then cast(' ~ source_prefix ~ 'playoff_round_number as ' ~ int64_type() ~ ') end'
        if 'playoff_round_number' in source_columns
        else 'cast(null as ' ~ int64_type() ~ ')'
    ) %}
    {% set round_name_expr = (
        playoff_round_number_from_name(
            "nullif(trim(cast(" ~ source_prefix ~ "playoff_round_name as " ~ varchar_type() ~ ")), '')"
        )
        if 'playoff_round_name' in source_columns
        else 'cast(null as ' ~ int64_type() ~ ')'
    ) %}
    {% set series_id_expr = (
        "nullif(trim(cast(" ~ source_prefix ~ "playoff_series_id as " ~ varchar_type() ~ ")), '')"
        if 'playoff_series_id' in source_columns
        else 'cast(null as ' ~ varchar_type() ~ ')'
    ) %}
    {% set series_game_expr = (
        'case when cast(' ~ source_prefix ~ 'playoff_series_game_number as ' ~ int64_type() ~ ') between 1 and 7 '
        'then cast(' ~ source_prefix ~ 'playoff_series_game_number as ' ~ int64_type() ~ ') end'
        if 'playoff_series_game_number' in source_columns
        else 'cast(null as ' ~ int64_type() ~ ')'
    ) %}
    coalesce({{ round_number_expr }}, {{ round_name_expr }}) as explicit_playoff_round_number,
    case
        when ({{ round_number_expr }}) is not null
            and ({{ round_name_expr }}) is not null
            and ({{ round_number_expr }}) != ({{ round_name_expr }})
        then true
        else false
    end as explicit_playoff_round_row_conflict,
    {{ series_id_expr }} as explicit_playoff_series_id,
    {{ series_game_expr }} as explicit_playoff_series_game_number
{%- endmacro %}

{% macro playoff_round_resolved_columns(game_id, season_type, round_expr, series_id_expr, series_game_expr, conflict_expr='false') -%}
    {% set fallback_round = 'cast(substr(' ~ game_id ~ ', 8, 1) as ' ~ int64_type() ~ ')' %}
    {% set resolved_round = 'coalesce(' ~ round_expr ~ ', ' ~ fallback_round ~ ')' %}
    {% set is_canonical_id = canonical_playoff_game_id(game_id) %}
    {% set is_canonical_playoff = season_type ~ " = 'Playoffs' and " ~ is_canonical_id %}
    {% set is_valid_playoff = (
        is_canonical_playoff
        ~ " and not coalesce(" ~ conflict_expr ~ ", false)"
    ) %}
    case when {{ is_valid_playoff }} then {{ resolved_round }} end as playoff_round_number,
    case when {{ is_valid_playoff }} then {{ playoff_round_name(resolved_round) }} end as playoff_round_name,
    case when {{ is_valid_playoff }} then coalesce({{ series_id_expr }}, substr({{ game_id }}, 1, 9)) end as playoff_series_id,
    case when {{ is_valid_playoff }} then coalesce({{ series_game_expr }}, cast(substr({{ game_id }}, 10, 1) as {{ int64_type() }})) end as playoff_series_game_number,
    case
        when not coalesce({{ is_canonical_id }}, false) then 'none'
        when {{ season_type }} is distinct from 'Playoffs' or coalesce({{ conflict_expr }}, false) then 'conflict'
        when ({{ round_expr }}) is not null then 'explicit'
        else 'game_id'
    end as playoff_round_source
{%- endmacro %}
