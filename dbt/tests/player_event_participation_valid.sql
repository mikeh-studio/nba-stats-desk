{% set participant_relation = var('game_participants_relation', none) if var is defined else none %}
{% if participant_relation is none and adapter is defined %}
{% set participant_relation = adapter.get_relation(
    database=env_var('BQ_PROJECT', env_var('GCP_PROJECT_ID', 'local-project')),
    schema=env_var('BQ_DATASET_GOLD', env_var('BQ_DATASET', 'nba_gold')),
    identifier='game_participants'
) %}
{% endif %}

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
verified_game_participants as (
    {% if participant_relation is not none %}
    select
        season,
        game_id,
        player_id,
        team_abbr,
        played
    from {{ participant_relation }}
    {% else %}
    select
        g.season,
        g.game_id,
        p.player_id,
        p.team_abbr,
        false as played
    from {{ ref('dim_game') }} g
    join {{ ref('fct_player_game_stats') }} p on 1 = 0
    {% endif %}
),
observed_playoff_players as (
    select distinct
        p.season,
        p.game_id,
        p.player_id,
        p.team_abbr
    from {{ ref('fct_player_game_stats') }} p
    join {{ ref('dim_game') }} g
        on p.season = g.season
       and p.game_id = g.game_id
    where p.season_type = 'Playoffs'
      and g.season_type = 'Playoffs'
      and g.playoff_round_number between 1 and 4
      and p.team_abbr in (g.home_team_abbr, g.away_team_abbr)
      and p.player_id is not null
),
resolved_game_participants as (
    select
        coalesce(gp.season, g.season) as season,
        gp.game_id,
        gp.player_id,
        gp.team_abbr,
        gp.played
    from verified_game_participants gp
    left join {{ ref('dim_game') }} g
        on gp.game_id = g.game_id
       and (gp.season is null or gp.season = g.season)
),
manifest_played_true as (
    select
        gp.season,
        gp.game_id,
        gp.player_id,
        gp.team_abbr
    from resolved_game_participants gp
    join {{ ref('dim_game') }} g
        on gp.season = g.season
       and gp.game_id = g.game_id
    where g.season_type = 'Playoffs'
      and g.playoff_round_number between 1 and 4
      and gp.played is true
      and gp.player_id is not null
      and gp.team_abbr in (g.home_team_abbr, g.away_team_abbr)
    group by 1, 2, 3, 4
),
manifest_played_false as (
    select
        gp.season,
        gp.game_id,
        gp.player_id,
        gp.team_abbr
    from resolved_game_participants gp
    where gp.played is false
      and gp.player_id is not null
      and gp.team_abbr is not null
    group by 1, 2, 3, 4
),
manifest_game_quality as (
    select
        g.season,
        g.game_id,
        count(*) as manifest_row_count,
        sum(
            case
                when gp.player_id is null
                  or gp.team_abbr is null
                  or trim(gp.team_abbr) = ''
                  or gp.played is null
                  or (gp.played is true and gp.team_abbr not in (g.home_team_abbr, g.away_team_abbr))
                then 1 else 0
            end
        ) as invalid_manifest_rows,
        count(distinct case when gp.played is true and gp.team_abbr = g.home_team_abbr then gp.player_id end) as home_manifest_players,
        count(distinct case when gp.played is true and gp.team_abbr = g.away_team_abbr then gp.player_id end) as away_manifest_players
    from {{ ref('dim_game') }} g
    join resolved_game_participants gp
        on g.season = gp.season
       and g.game_id = gp.game_id
    where g.season_type = 'Playoffs'
      and g.playoff_round_number between 1 and 4
    group by 1, 2
),
manifest_game_mismatches as (
    select
        coalesce(o.season, m.season) as season,
        coalesce(o.game_id, m.game_id) as game_id,
        sum(
            case
                when o.player_id is null
                  or m.player_id is null
                  or dnp.player_id is not null
                then 1 else 0
            end
        ) as mismatch_count
    from observed_playoff_players o
    full outer join manifest_played_true m
        on o.season = m.season
       and o.game_id = m.game_id
       and o.player_id = m.player_id
       and o.team_abbr = m.team_abbr
    left join manifest_played_false dnp
        on o.season = dnp.season
       and o.game_id = dnp.game_id
       and o.player_id = dnp.player_id
       and o.team_abbr = dnp.team_abbr
    group by 1, 2
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
        coalesce(q.manifest_row_count, 0) > 0
            and coalesce(q.invalid_manifest_rows, 0) = 0
            and coalesce(q.home_manifest_players, 0) > 0
            and coalesce(q.away_manifest_players, 0) > 0
            and coalesce(mm.mismatch_count, 0) = 0 as both_teams_observed
    from {{ ref('dim_game') }} g
    left join manifest_game_quality q
        on g.season = q.season and g.game_id = q.game_id
    left join manifest_game_mismatches mm
        on g.season = mm.season and g.game_id = mm.game_id
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
        ) as reverse_game_order,
        lag(game_date) over (
            partition by season, round_number, team_a, team_b
            order by series_game_number, game_id
        ) as previous_game_date
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
        sum(case when previous_game_date >= game_date then 1 else 0 end) as out_of_order_games,
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
            and s.out_of_order_games = 0
            and s.games = s.covered_games
            and s.games = s.team_a_wins + s.team_b_wins
            and ((s.final_winner = s.team_a and s.team_a_wins = 4)
                or (s.final_winner = s.team_b and s.team_b_wins = 4))
            then 1 else 0 end) = count(*)
),
manifest_excluded_players as (
    select distinct
        c.season,
        c.round_number,
        gp.player_id
    from completed_rounds c
    join resolved_game_participants gp
        on c.season = gp.season
    left join {{ ref('fct_player_game_stats') }} a
        on gp.season = a.season
       and gp.game_id = a.game_id
       and gp.player_id = a.player_id
       and a.season_type in ('Regular Season', 'Playoffs')
    left join round_teams rt
        on c.season = rt.season
       and c.round_number = rt.round_number
       and gp.team_abbr = rt.team_abbr
    left join round_games rg
        on c.season = rg.season
       and c.round_number = rg.round_number
       and gp.game_id = rg.game_id
    where gp.player_id is not null
      and (
          (gp.played is true and a.player_id is null)
          or (gp.played is false and a.player_id is not null)
          or rt.team_abbr is not null
          or (gp.played is true and rg.game_id is not null)
      )
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
    left join manifest_excluded_players me
        on c.season = me.season
       and c.round_number = me.round_number
       and p.player_id = me.player_id
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
        or max(case when me.player_id is not null then 1 else 0 end) > 0
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
    left join completed_rounds c
        on p.season = c.season and p.event_name = c.event_name
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
    group by 1, 2, 3, c.event_name, p.is_verified_complete_cohort
    having count(a.player_id) = 0
        or (p.is_verified_complete_cohort is true and c.event_name is null)
)
select * from duplicates
union all
select * from invalid_nonparticipants
union all
select * from invalid_participants
