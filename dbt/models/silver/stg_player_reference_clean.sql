{{ config(
    materialized='table',
    schema=env_var('BQ_DATASET_SILVER', env_var('BQ_DATASET', 'nba_silver'))
) }}

select
    cast(player_id as {{ int64_type() }}) as player_id,
    cast(first_name as {{ varchar_type() }}) as first_name,
    cast(last_name as {{ varchar_type() }}) as last_name,
    cast(player_name as {{ varchar_type() }}) as player_name,
    cast(player_slug as {{ varchar_type() }}) as player_slug,
    cast(birthdate as date) as birthdate,
    cast(school as {{ varchar_type() }}) as school,
    cast(country as {{ varchar_type() }}) as country,
    cast(last_affiliation as {{ varchar_type() }}) as last_affiliation,
    cast(height as {{ varchar_type() }}) as height,
    cast(weight as {{ int64_type() }}) as weight,
    cast(wingspan as {{ float64_type() }}) as wingspan,
    cast(wingspan_ft_in as {{ varchar_type() }}) as wingspan_ft_in,
    cast(season_exp as {{ int64_type() }}) as season_exp,
    cast(jersey as {{ varchar_type() }}) as jersey,
    cast(position as {{ varchar_type() }}) as position,
    cast(roster_status as boolean) as roster_status,
    cast(team_id as {{ int64_type() }}) as team_id,
    cast(team_name as {{ varchar_type() }}) as team_name,
    upper(cast(team_abbr as {{ varchar_type() }})) as team_abbr,
    cast(team_code as {{ varchar_type() }}) as team_code,
    cast(team_city as {{ varchar_type() }}) as team_city,
    cast(from_year as {{ int64_type() }}) as from_year,
    cast(to_year as {{ int64_type() }}) as to_year,
    cast(draft_year as {{ varchar_type() }}) as draft_year,
    cast(draft_round as {{ varchar_type() }}) as draft_round,
    cast(draft_number as {{ varchar_type() }}) as draft_number,
    cast(ingested_at_utc as timestamp) as ingested_at_utc
from {{ source('bronze', 'raw_player_reference') }}
