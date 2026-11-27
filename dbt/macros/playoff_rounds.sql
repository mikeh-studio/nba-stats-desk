{% macro playoff_round_columns() %}
    {{ return([
        'playoff_round_number',
        'playoff_round_name',
        'playoff_series_id',
        'playoff_series_game_number'
    ]) }}
{% endmacro %}

{% macro playoff_round_labels() %}
    {{ return(['First Round', 'Conference Semifinals', 'Conference Finals', 'NBA Finals']) }}
{% endmacro %}

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
    {% set fields = playoff_round_columns() %}
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
    end as {{ field }}{% if not loop.last %},{% endif %}
    {% endfor %}
{%- endmacro %}
