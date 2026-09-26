{% macro warehouse_season() -%}
  {% set season = var('nba_season', '2025-26') %}
  {% if season not in ['2023-24', '2024-25', '2025-26', '2026-27'] %}
    {{ exceptions.raise_compiler_error('Unsupported nba_season: ' ~ season) }}
  {% endif %}
  {{ return(season) }}
{%- endmacro %}

{% macro warehouse_season_start() -%}
  {{ return(warehouse_season()[:4] ~ '-07-01') }}
{%- endmacro %}

{% macro warehouse_season_end() -%}
  {{ return(((warehouse_season()[:4] | int) + 1) ~ '-06-30') }}
{%- endmacro %}

{% macro warehouse_today() -%}
  {% set as_of = var('nba_as_of_date', none) %}
  {% if as_of %}
    {% set parsed = modules.datetime.datetime.strptime(as_of, '%Y-%m-%d') %}
    {{ return("date('" ~ parsed.strftime('%Y-%m-%d') ~ "')") }}
  {% endif %}
  {{ return('current_date()' if target.type == 'bigquery' else 'current_date') }}
{%- endmacro %}
