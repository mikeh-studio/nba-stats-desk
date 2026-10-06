-- Completed team-game membership and scores must agree with player facts.
with players as (
    select game_id, team_abbr, count(*) as appearances,
        count(pts) as known_points, sum(pts) as pts
    from {{ ref('fct_player_game_stats') }}
    group by 1, 2
),
teams as (
    select game_id, team_abbr, count(*) as records, max(team_pts) as pts
    from {{ ref('fct_team_game_scores') }}
    group by 1, 2
)
select coalesce(p.game_id, t.game_id) as game_id,
    coalesce(p.team_abbr, t.team_abbr) as team_abbr
from players p
full outer join teams t using (game_id, team_abbr)
where p.game_id is null or t.game_id is null or t.records <> 1
    or p.known_points <> p.appearances or t.pts is null or p.pts <> t.pts
