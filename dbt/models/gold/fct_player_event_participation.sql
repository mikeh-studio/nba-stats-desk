{{ config(
    materialized='table',
    schema=env_var('BQ_DATASET_GOLD', env_var('BQ_DATASET', 'nba_gold'))
) }}

with round_games as (
    select
        season,
        game_id,
        playoff_round_number as round_number,
        playoff_round_name as event_name,
        home_team_abbr,
        away_team_abbr,
        winning_team_abbr
    from {{ ref('dim_game') }}
    where season_type = 'Playoffs'
      and playoff_round_number between 1 and 4
      and playoff_round_name = {{ playoff_round_name('playoff_round_number') }}
),
series_results as (
    select
        season,
        round_number,
        event_name,
        least(home_team_abbr, away_team_abbr) as team_a,
        greatest(home_team_abbr, away_team_abbr) as team_b,
        count(*) as games,
        sum(case when winning_team_abbr = least(home_team_abbr, away_team_abbr)
            then 1 else 0 end) as team_a_wins,
        sum(case when winning_team_abbr = greatest(home_team_abbr, away_team_abbr)
            then 1 else 0 end) as team_b_wins
    from round_games
    group by 1, 2, 3, 4, 5
),
round_teams as (
    select season, round_number, home_team_abbr as team_abbr from round_games
    union distinct
    select season, round_number, away_team_abbr as team_abbr from round_games
),
round_team_counts as (
    select season, round_number, count(*) as team_count
    from round_teams
    group by 1, 2
),
completed_rounds as (
    select s.season, s.round_number, s.event_name
    from series_results s
    join round_team_counts t using (season, round_number)
    group by 1, 2, 3, t.team_count
    having count(*) = power(2, 4 - s.round_number)
       and t.team_count = 2 * count(*)
       and sum(case when s.games between 4 and 7
            and s.team_a != s.team_b
            and s.games = s.team_a_wins + s.team_b_wins
            and greatest(s.team_a_wins, s.team_b_wins) = 4
            then 1 else 0 end) = count(*)
),
player_appearances as (
    select
        p.season,
        p.player_id,
        p.player_name,
        p.team_abbr,
        p.season_type,
        p.game_id,
        p.game_date,
        g.round_number,
        g.event_name
    from {{ ref('fct_player_game_stats') }} p
    left join round_games g on p.season = g.season and p.game_id = g.game_id
    where p.season_type in ('Regular Season', 'Playoffs')
),
player_names as (
    select season, player_id, player_name
    from player_appearances
    qualify row_number() over (
        partition by season, player_id
        order by game_date desc, game_id desc
    ) = 1
),
participation as (
    select distinct
        p.season,
        p.event_name,
        p.player_id,
        true as participated,
        c.round_number is not null as is_verified_complete_cohort,
        'A player-game appearance was recorded in this round.' as reason
    from player_appearances p
    left join completed_rounds c
        on p.season = c.season and p.round_number = c.round_number
    where p.round_number is not null

    union all

    select
        c.season,
        c.event_name,
        p.player_id,
        false as participated,
        true as is_verified_complete_cohort,
        concat('Recorded season teams did not reach the ', c.event_name, '.') as reason
    from completed_rounds c
    join player_appearances p on c.season = p.season
    left join round_teams t
        on c.season = t.season and c.round_number = t.round_number and p.team_abbr = t.team_abbr
    group by c.season, c.event_name, p.player_id
    having sum(case when p.season_type = 'Regular Season' or p.round_number < c.round_number
        then 1 else 0 end) > 0
       and sum(case when t.team_abbr is not null
            or p.team_abbr is null or trim(p.team_abbr) = ''
            or p.round_number = c.round_number
            or (p.season_type = 'Playoffs' and p.round_number is null)
            then 1 else 0 end) = 0
)
select p.season, p.event_name, p.player_id, n.player_name, p.participated,
    p.reason, p.is_verified_complete_cohort
from participation p
join player_names n using (season, player_id)
