{% macro context_day_start(expression) %}
    cast({{ expression }} as timestamp)
{% endmacro %}
