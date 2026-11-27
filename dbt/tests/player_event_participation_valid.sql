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
playoff_player_teams as (
    select distinct season, game_id, team_abbr
    from {{ ref('fct_player_game_stats') }}
    where season_type = 'Playoffs'
),
invalid_nonparticipants as (
    select distinct p.season, p.event_name, p.player_id
    from participation p
    left join regular_seasons r
        on p.season = r.season
    left join {{ ref('dim_game') }} g
        on p.season = g.season and p.event_name = g.playoff_round_name
        and g.season_type = 'Playoffs'
    left join {{ ref('fct_player_game_stats') }} a
        on g.season = a.season and g.game_id = a.game_id and p.player_id = a.player_id
    left join playoff_player_teams home
        on g.season = home.season and g.game_id = home.game_id and g.home_team_abbr = home.team_abbr
    left join playoff_player_teams away
        on g.season = away.season and g.game_id = away.game_id and g.away_team_abbr = away.team_abbr
    where not p.participated
      and (a.player_id is not null or p.is_verified_complete_cohort is not true
            or r.season is null or home.team_abbr is null or away.team_abbr is null)
)
select * from duplicates
union all
select * from invalid_nonparticipants
