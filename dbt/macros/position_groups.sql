{% macro listed_position_groups(position) -%}
    case
        when {{ position }} in ('G', 'PG', 'SG') then ['Guard']
        when {{ position }} in ('F', 'SF', 'PF') then ['Wing']
        when {{ position }} = 'C' then ['Big']
        when {{ position }} in ('G-F', 'F-G') then ['Guard', 'Wing']
        when {{ position }} in ('F-C', 'C-F') then ['Wing', 'Big']
        else array<string>[]
    end
{%- endmacro %}
