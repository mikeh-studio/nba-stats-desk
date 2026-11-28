with participation as (
    select * from {{ ref('fct_player_event_participation') }}
),
duplicates as (
    select season, event_name, player_id
    from participation
    group by 1, 2, 3
    having count(*) > 1
),
regular_seasons as (
    select distinct season
    from {{ ref('fct_player_game_stats') }}
    where season_type = 'Regular Season'
),
playoff_team_player_counts as (
    select season, game_id, team_abbr, count(distinct player_id) as player_count
    from {{ ref('fct_player_game_stats') }}
    where season_type = 'Playoffs'
    group by 1, 2, 3
),
round_games as (
    select
        g.season,
        g.game_id,
        g.game_date,
        g.playoff_round_number as round_number,
        g.playoff_round_name as event_name,
        g.playoff_series_game_number as series_game_number,
        g.home_team_abbr,
        g.away_team_abbr,
        least(g.home_team_abbr, g.away_team_abbr) as team_a,
        greatest(g.home_team_abbr, g.away_team_abbr) as team_b,
        g.winning_team_abbr,
        coalesce(home.player_count, 0) >= 5
            and coalesce(away.player_count, 0) >= 5 as both_teams_observed
    from {{ ref('dim_game') }} g
    left join playoff_team_player_counts home
        on g.season = home.season and g.game_id = home.game_id and g.home_team_abbr = home.team_abbr
    left join playoff_team_player_counts away
        on g.season = away.season and g.game_id = away.game_id and g.away_team_abbr = away.team_abbr
    where g.season_type = 'Playoffs'
      and g.playoff_round_number between 1 and 4
      and g.playoff_round_name = {{ playoff_round_name('g.playoff_round_number') }}
),
series_games as (
    select
        *,
        row_number() over (
            partition by season, round_number, team_a, team_b
            order by game_date desc, game_id desc
        ) as reverse_game_order
    from round_games
),
series_results as (
    select
        season,
        round_number,
        event_name,
        team_a,
        team_b,
        count(*) as games,
        count(game_date) as dated_games,
        count(case when series_game_number between 1 and 7 then 1 end) as numbered_games,
        count(distinct case when series_game_number between 1 and 7 then series_game_number end) as distinct_game_numbers,
        min(series_game_number) as min_game_number,
        max(series_game_number) as max_game_number,
        sum(case when both_teams_observed then 1 else 0 end) as covered_games,
        sum(case when winning_team_abbr = team_a then 1 else 0 end) as team_a_wins,
        sum(case when winning_team_abbr = team_b then 1 else 0 end) as team_b_wins,
        max(case when reverse_game_order = 1 then winning_team_abbr end) as final_winner
    from series_games
    group by 1, 2, 3, 4, 5
),
round_teams as (
    select season, round_number, home_team_abbr as team_abbr from round_games
    union distinct
    select season, round_number, away_team_abbr as team_abbr from round_games
),
unclassified_playoff_teams as (
    select season, home_team_abbr as team_abbr
    from {{ ref('dim_game') }}
    where season_type = 'Playoffs'
      and (
          playoff_round_number is null
          or playoff_round_number not between 1 and 4
          or playoff_round_name is null
          or playoff_round_name != {{ playoff_round_name('playoff_round_number') }}
      )
    union distinct
    select season, away_team_abbr as team_abbr
    from {{ ref('dim_game') }}
    where season_type = 'Playoffs'
      and (
          playoff_round_number is null
          or playoff_round_number not between 1 and 4
          or playoff_round_name is null
          or playoff_round_name != {{ playoff_round_name('playoff_round_number') }}
      )
    union distinct
    select p.season, p.team_abbr
    from {{ ref('fct_player_game_stats') }} p
    left join round_games g
        on p.season = g.season and p.game_id = g.game_id
    where p.season_type = 'Playoffs'
      and g.game_id is null
),
round_team_counts as (
    select
        rt.season,
        rt.round_number,
        count(*) as team_count,
        count(u.team_abbr) as unclassified_team_count
    from round_teams rt
    left join unclassified_playoff_teams u
        on rt.season = u.season and rt.team_abbr = u.team_abbr
    group by 1, 2
),
completed_rounds as (
    select s.season, s.round_number, s.event_name
    from series_results s
    join regular_seasons r
        on s.season = r.season
    join round_team_counts t
        on s.season = t.season and s.round_number = t.round_number
    group by 1, 2, 3, t.team_count, t.unclassified_team_count
    having count(*) = power(2, 4 - s.round_number)
       and t.team_count = 2 * count(*)
       and t.unclassified_team_count = 0
       and sum(case when s.games between 4 and 7
            and s.team_a != s.team_b
            and s.games = s.dated_games
            and s.games = s.numbered_games
            and s.games = s.distinct_game_numbers
            and s.min_game_number = 1
            and s.max_game_number = s.games
            and s.games = s.covered_games
            and s.games = s.team_a_wins + s.team_b_wins
            and ((s.final_winner = s.team_a and s.team_a_wins = 4)
                or (s.final_winner = s.team_b and s.team_b_wins = 4))
            then 1 else 0 end) = count(*)
),
invalid_nonparticipants as (
    select p.season, p.event_name, p.player_id
    from participation p
    left join regular_seasons r
        on p.season = r.season
    left join completed_rounds c
        on p.season = c.season and p.event_name = c.event_name
    left join {{ ref('fct_player_game_stats') }} a
        on p.season = a.season
       and p.player_id = a.player_id
       and a.season_type in ('Regular Season', 'Playoffs')
    left join round_games rg
        on a.season = rg.season and a.game_id = rg.game_id
    left join round_teams rt
        on c.season = rt.season
       and c.round_number = rt.round_number
       and a.team_abbr = rt.team_abbr
    where not p.participated
    group by
        p.season,
        p.event_name,
        p.player_id,
        p.is_verified_complete_cohort,
        r.season,
        c.event_name,
        c.round_number
    having p.is_verified_complete_cohort is not true
        or r.season is null
        or c.event_name is null
        or sum(
            case
                when a.season_type = 'Regular Season'
                  or rg.round_number < c.round_number
                then 1 else 0
            end
        ) = 0
        or sum(
            case
                when rt.team_abbr is not null
                  or (a.player_id is not null and (a.team_abbr is null or trim(a.team_abbr) = ''))
                  or rg.round_number = c.round_number
                  or (a.season_type = 'Playoffs' and rg.round_number is null)
                then 1 else 0
            end
        ) > 0
),
invalid_participants as (
    select distinct p.season, p.event_name, p.player_id
    from participation p
    left join {{ ref('dim_game') }} g
        on p.season = g.season and p.event_name = g.playoff_round_name
        and g.season_type = 'Playoffs'
    left join {{ ref('fct_player_game_stats') }} a
        on g.season = a.season
       and g.game_id = a.game_id
       and p.player_id = a.player_id
       and a.season_type = 'Playoffs'
       and a.team_abbr in (g.home_team_abbr, g.away_team_abbr)
    where p.participated
    group by 1, 2, 3
    having count(a.player_id) = 0
)
select * from duplicates
union all
select * from invalid_nonparticipants
union all
select * from invalid_participants
