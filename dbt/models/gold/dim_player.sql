{{ config(
    materialized='table',
    schema=env_var('BQ_DATASET_GOLD', env_var('BQ_DATASET', 'nba_gold'))
) }}

with latest_profile as (
    select
        player_id,
        first_name,
        last_name,
        player_name,
        player_slug,
        birthdate,
        school,
        country,
        last_affiliation,
        height,
        weight,
        wingspan,
        wingspan_ft_in,
        season_exp,
        jersey,
        position,
        roster_status,
        team_id,
        team_name,
        team_abbr,
        team_code,
        team_city,
        from_year,
        to_year,
        draft_year,
        draft_round,
        draft_number,
        ingested_at_utc,
        row_number() over (
            partition by player_id
            order by ingested_at_utc desc, player_name
        ) as row_num
    from {{ ref('stg_player_reference_clean') }}
),
latest_seen as (
    select
        player_id,
        max(ingested_at_utc) as last_seen_at_utc
    from {{ ref('int_player_game_enriched') }}
    group by 1
)
select
    p.player_id,
    p.player_name,
    p.first_name,
    p.last_name,
    p.player_slug,
    p.birthdate,
    p.school,
    p.country,
    p.last_affiliation,
    p.height,
    p.weight,
    p.wingspan,
    p.wingspan_ft_in,
    p.season_exp,
    p.jersey,
    p.position,
    p.roster_status,
    p.team_id,
    p.team_name,
    p.team_abbr as latest_team_abbr,
    p.team_code,
    p.team_city,
    p.from_year,
    p.to_year,
    p.draft_year,
    p.draft_round,
    p.draft_number,
    '2025-26' as latest_season,
    coalesce(s.last_seen_at_utc, p.ingested_at_utc) as last_seen_at_utc,
    p.ingested_at_utc as last_profile_refresh_at_utc
from latest_profile p
left join latest_seen s
    on p.player_id = s.player_id
where p.row_num = 1
