{{ config(materialized='view', schema=env_var('BQ_DATASET_GOLD', env_var('BQ_DATASET', 'nba_gold'))) }}

-- Box-score coverage is a diagnostic, not proof of upstream completeness.
with totals as (
    select season, season_type, game_id, game_date, team_abbr,
        min(opponent_abbr) as opponent_abbr,
        sum(pts) as points, sum(fgm) as fgm, sum(fga) as fga,
        sum(fg3m) as fg3m, sum(min) as player_minutes,
        case when count(*) >= 5 and count(distinct opponent_abbr) = 1
            and count(pts) = count(*) and count(min) = count(*)
            -- League logs round each appearance to whole minutes. The sum can
            -- differ by at most half a minute per player (verified vs team logs).
            and sum(min) >= 240 - count(*) * 0.5
            and abs(sum(min) - (240 + 25 * round((sum(min) - 240) / 25.0))) <= count(*) * 0.5
            then 1 else 0 end as box_coverage_ok,
        case when count(fgm) = count(*) and count(fga) = count(*)
            and count(fg3m) = count(*) and sum(fga) > 0
            and min(fga - fgm) >= 0 and min(fgm - fg3m) >= 0
            then 1 else 0 end as shooting_coverage_ok
    from {{ ref('fct_player_game_stats') }}
    group by season, season_type, game_id, game_date, team_abbr
)
select t.*, o.points as points_allowed, o.fgm as opponent_fgm,
    o.fga as opponent_fga, o.fg3m as opponent_fg3m,
    case when t.box_coverage_ok = 1 and o.box_coverage_ok = 1
        and t.points != o.points then 1 else 0 end as comparison_coverage_ok,
    case when t.box_coverage_ok = 1 and o.box_coverage_ok = 1
        and o.shooting_coverage_ok = 1 then 1 else 0 end as defense_shooting_coverage_ok,
    case when t.points > o.points then 1 else 0 end as won
from totals t
left join totals o on t.season = o.season and t.season_type = o.season_type
    and t.game_id = o.game_id and t.opponent_abbr = o.team_abbr
    and o.opponent_abbr = t.team_abbr
