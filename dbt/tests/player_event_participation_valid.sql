with participation as (
    select * from {{ ref('fct_player_event_participation') }}
),
duplicates as (
    select season, event_name, player_id
    from participation
    group by 1, 2, 3
    having count(*) > 1
),
invalid_nonparticipants as (
    select distinct p.season, p.event_name, p.player_id
    from participation p
    left join {{ ref('dim_game') }} g
        on p.season = g.season and p.event_name = g.playoff_round_name
    left join {{ ref('fct_player_game_stats') }} a
        on g.season = a.season and g.game_id = a.game_id and p.player_id = a.player_id
    where not p.participated
      and (a.player_id is not null or p.is_verified_complete_cohort is not true)
)
select * from duplicates
union all
select * from invalid_nonparticipants
