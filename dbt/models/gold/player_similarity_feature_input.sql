{{ config(
    materialized='table',
    schema=env_var('BQ_DATASET_GOLD', env_var('BQ_DATASET', 'nba_gold'))
) }}

{% set today = 'current_date()' if target.type == 'bigquery' else 'current_date' %}

with recent_form as (
    select
        season,
        player_id,
        player_name,
        team_abbr,
        season_games,
        avg_min_last_5,
        avg_min_last_10,
        fantasy_points_last_5,
        fantasy_points_last_10,
        pts_last_5,
        pts_last_10,
        reb_last_5,
        reb_last_10,
        ast_last_5,
        ast_last_10,
        stl_last_5,
        stl_last_10,
        blk_last_5,
        blk_last_10,
        fg3m_last_5,
        fg3m_last_10,
        tov_last_5,
        tov_last_10,
        fantasy_points_delta_vs_season,
        minutes_delta_vs_season
    from {{ ref('player_recent_form') }}
),
category_profile as (
    select
        season,
        player_id,
        player_name,
        latest_team_abbr,
        games_sampled,
        avg_pts,
        avg_reb,
        avg_ast,
        avg_stl,
        avg_blk,
        avg_fg3m,
        avg_tov,
        avg_min,
        avg_fantasy_points_simple,
        z_pts,
        z_reb,
        z_ast,
        z_stl,
        z_blk,
        z_fg3m,
        z_tov,
        z_min,
        z_fantasy_points_simple
    from {{ ref('player_category_profile') }}
),
player_dimension as (
    select
        player_id,
        latest_season,
        latest_team_abbr,
        position,
        safe_cast(split(coalesce(trim(height), ''), '-')[safe_offset(0)] as {{ int64_type() }}) * 12
            + safe_cast(split(coalesce(trim(height), ''), '-')[safe_offset(1)] as {{ int64_type() }}) as height_inches,
        weight as weight_lbs,
        season_exp
    from {{ ref('dim_player') }}
),
scoring_contribution_base as (
    select
        season,
        player_id,
        row_number() over (
            partition by season, player_id
            order by game_date desc, game_id desc
        ) as game_num,
        player_points_share_of_team,
        player_points_share_of_game
    from {{ ref('fct_player_scoring_contribution') }}
),
scoring_contribution as (
    select
        season,
        player_id,
        round(avg(player_points_share_of_team), 4) as season_points_share_of_team,
        round(avg(case when game_num <= 10 then player_points_share_of_team end), 4) as recent_points_share_of_team,
        round(avg(player_points_share_of_game), 4) as season_points_share_of_game,
        round(avg(case when game_num <= 10 then player_points_share_of_game end), 4) as recent_points_share_of_game
    from scoring_contribution_base
    group by 1, 2
),
style_profile as (
    select
        season,
        player_id,
        round(avg(fga), 2) as season_avg_fga,
        round({{ safe_divide('sum(fgm)', 'nullif(sum(fga), 0)') }}, 4) as season_fg_pct,
        round({{ safe_divide('sum(fg3a)', 'nullif(sum(fga), 0)') }}, 4) as season_fg3a_rate,
        round({{ safe_divide('sum(fta)', 'nullif(sum(fga), 0)') }}, 4) as season_fta_rate,
        round(
            {{ safe_divide('sum(pts)', 'nullif(2 * (sum(fga) + 0.44 * sum(fta)), 0)') }},
            4
        ) as season_ts_pct,
        round({{ safe_divide('sum(ast)', 'nullif(sum(tov), 0)') }}, 4) as season_ast_to_tov
    from {{ ref('fct_player_game_stats') }}
    group by 1, 2
),
team_box_score_by_game as (
    select
        season,
        game_id,
        team_abbr,
        sum(coalesce(pts, 0)) as team_player_pts,
        sum(coalesce(fga, 0)) as team_player_fga,
        sum(coalesce(ast, 0)) as team_player_ast,
        sum(coalesce(tov, 0)) as team_player_tov,
        sum(coalesce(reb, 0)) as team_player_reb,
        sum(coalesce(stl, 0)) as team_player_stl,
        sum(coalesce(blk, 0)) as team_player_blk
    from {{ ref('fct_player_game_stats') }}
    group by 1, 2, 3
),
team_contribution_profile as (
    select
        p.season,
        p.team_abbr,
        p.player_id,
        round({{ safe_divide('sum(coalesce(p.pts, 0))', 'nullif(sum(coalesce(t.team_player_pts, 0)), 0)') }}, 4) as team_points_contribution_rate,
        round({{ safe_divide('sum(coalesce(p.fga, 0))', 'nullif(sum(coalesce(t.team_player_fga, 0)), 0)') }}, 4) as team_fga_contribution_rate,
        round({{ safe_divide('sum(coalesce(p.ast, 0))', 'nullif(sum(coalesce(t.team_player_ast, 0)), 0)') }}, 4) as team_ast_contribution_rate,
        round({{ safe_divide('sum(coalesce(p.tov, 0))', 'nullif(sum(coalesce(t.team_player_tov, 0)), 0)') }}, 4) as team_tov_contribution_rate,
        round({{ safe_divide('sum(coalesce(p.reb, 0))', 'nullif(sum(coalesce(t.team_player_reb, 0)), 0)') }}, 4) as team_reb_contribution_rate,
        round({{ safe_divide('sum(coalesce(p.stl, 0))', 'nullif(sum(coalesce(t.team_player_stl, 0)), 0)') }}, 4) as team_stl_contribution_rate,
        round({{ safe_divide('sum(coalesce(p.blk, 0))', 'nullif(sum(coalesce(t.team_player_blk, 0)), 0)') }}, 4) as team_blk_contribution_rate
    from {{ ref('fct_player_game_stats') }} p
    left join team_box_score_by_game t
        on p.season = t.season
       and p.game_id = t.game_id
       and p.team_abbr = t.team_abbr
    group by 1, 2, 3
),
team_contribution_scored as (
    select
        *,
        round(
            (
                coalesce(team_points_contribution_rate, 0)
                + coalesce(team_fga_contribution_rate, 0)
                + coalesce(team_ast_contribution_rate, 0)
            ) / 3.0,
            4
        ) as team_offense_contribution_rate,
        round(
            (
                coalesce(team_reb_contribution_rate, 0)
                + coalesce(team_stl_contribution_rate, 0)
                + coalesce(team_blk_contribution_rate, 0)
            ) / 3.0,
            4
        ) as team_defense_contribution_rate
    from team_contribution_profile
),
team_contribution_summary as (
    select
        *,
        row_number() over (
            partition by season, team_abbr
            order by
                team_offense_contribution_rate desc,
                team_ast_contribution_rate desc,
                team_points_contribution_rate desc,
                player_id
        ) as team_offense_contribution_rank,
        row_number() over (
            partition by season, team_abbr
            order by
                team_defense_contribution_rate desc,
                team_blk_contribution_rate desc,
                team_stl_contribution_rate desc,
                player_id
        ) as team_defense_contribution_rank
    from team_contribution_scored
),
shot_location_profile as (
    select
        season,
        player_id,
        shot_location_fga,
        shot_rim_rate,
        shot_paint_non_ra_rate,
        shot_midrange_rate,
        shot_corner3_rate,
        shot_above_break3_rate,
        shot_rim_fg_pct,
        shot_corner3_fg_pct
    from {{ ref('player_shot_location_profile') }}
),
sequenced_player_games as (
    select
        season,
        player_id,
        game_id,
        game_date,
        pts,
        fga,
        fta,
        min as minutes,
        row_number() over (
            partition by season, player_id
            order by game_date, game_id
        ) as player_game_number,
        count(*) over (
            partition by season, player_id
        ) as player_season_games
    from {{ ref('fct_player_game_stats') }}
),
split_game_stats as (
    select
        *,
        case
            when player_game_number <= cast(ceil(player_season_games / 2.0) as {{ int64_type() }})
                then 'first_half'
            else 'second_half'
        end as season_half
    from sequenced_player_games
),
season_split_profile as (
    select
        season,
        player_id,
        {{ countif("season_half = 'first_half'") }} as first_half_games,
        {{ countif("season_half = 'second_half'") }} as second_half_games,
        round(avg(case when season_half = 'first_half' then pts end), 2) as first_half_avg_pts,
        round(avg(case when season_half = 'second_half' then pts end), 2) as second_half_avg_pts,
        round(avg(case when season_half = 'first_half' then minutes end), 2) as first_half_avg_min,
        round(avg(case when season_half = 'second_half' then minutes end), 2) as second_half_avg_min,
        round(
            {{ safe_divide(
                "sum(case when season_half = 'first_half' then pts else 0 end)",
                "nullif(2 * (sum(case when season_half = 'first_half' then fga else 0 end) + 0.44 * sum(case when season_half = 'first_half' then fta else 0 end)), 0)"
            ) }},
            4
        ) as first_half_ts_pct,
        round(
            {{ safe_divide(
                "sum(case when season_half = 'second_half' then pts else 0 end)",
                "nullif(2 * (sum(case when season_half = 'second_half' then fga else 0 end) + 0.44 * sum(case when season_half = 'second_half' then fta else 0 end)), 0)"
            ) }},
            4
        ) as second_half_ts_pct
    from split_game_stats
    group by 1, 2
)
select
    f.season,
    {{ today }} as as_of_date,
    f.player_id,
    f.player_name,
    coalesce(d.latest_team_abbr, c.latest_team_abbr, f.team_abbr) as team_abbr,
    d.position,
    d.height_inches,
    d.weight_lbs,
    d.season_exp,
    coalesce(c.games_sampled, f.season_games, 0) as games_sampled,
    case
        when coalesce(c.games_sampled, f.season_games, 0) >= 10 then 'ready'
        when coalesce(c.games_sampled, f.season_games, 0) >= 5 then 'limited_sample'
        else 'insufficient_sample'
    end as sample_status,
    coalesce(c.avg_pts, 0) as season_avg_pts,
    coalesce(sp.season_avg_fga, 0) as season_avg_fga,
    coalesce(sp.season_fg_pct, 0) as season_fg_pct,
    coalesce(sp.season_ts_pct, 0) as season_ts_pct,
    coalesce(sp.season_fg3a_rate, 0) as season_fg3a_rate,
    coalesce(sp.season_fta_rate, 0) as season_fta_rate,
    coalesce(sp.season_ast_to_tov, 0) as season_ast_to_tov,
    coalesce(tc.team_points_contribution_rate, 0) as team_points_contribution_rate,
    coalesce(tc.team_fga_contribution_rate, 0) as team_fga_contribution_rate,
    coalesce(tc.team_ast_contribution_rate, 0) as team_ast_contribution_rate,
    coalesce(tc.team_tov_contribution_rate, 0) as team_tov_contribution_rate,
    coalesce(tc.team_offense_contribution_rate, 0) as team_offense_contribution_rate,
    coalesce(tc.team_offense_contribution_rank, 999) as team_offense_contribution_rank,
    coalesce(tc.team_reb_contribution_rate, 0) as team_reb_contribution_rate,
    coalesce(tc.team_stl_contribution_rate, 0) as team_stl_contribution_rate,
    coalesce(tc.team_blk_contribution_rate, 0) as team_blk_contribution_rate,
    coalesce(tc.team_defense_contribution_rate, 0) as team_defense_contribution_rate,
    coalesce(tc.team_defense_contribution_rank, 999) as team_defense_contribution_rank,
    coalesce(sl.shot_location_fga, 0) as shot_location_fga,
    coalesce(sl.shot_rim_rate, 0) as shot_rim_rate,
    coalesce(sl.shot_paint_non_ra_rate, 0) as shot_paint_non_ra_rate,
    coalesce(sl.shot_midrange_rate, 0) as shot_midrange_rate,
    coalesce(sl.shot_corner3_rate, 0) as shot_corner3_rate,
    coalesce(sl.shot_above_break3_rate, 0) as shot_above_break3_rate,
    coalesce(sl.shot_rim_fg_pct, 0) as shot_rim_fg_pct,
    coalesce(sl.shot_corner3_fg_pct, 0) as shot_corner3_fg_pct,
    coalesce(h.first_half_games, 0) as first_half_games,
    coalesce(h.second_half_games, 0) as second_half_games,
    coalesce(h.first_half_avg_pts, c.avg_pts, 0) as first_half_avg_pts,
    coalesce(h.second_half_avg_pts, c.avg_pts, 0) as second_half_avg_pts,
    coalesce(h.first_half_avg_min, c.avg_min, 0) as first_half_avg_min,
    coalesce(h.second_half_avg_min, c.avg_min, 0) as second_half_avg_min,
    coalesce(h.first_half_ts_pct, sp.season_ts_pct, 0) as first_half_ts_pct,
    coalesce(h.second_half_ts_pct, sp.season_ts_pct, 0) as second_half_ts_pct,
    round(
        coalesce(h.second_half_avg_pts, c.avg_pts, 0)
        - coalesce(h.first_half_avg_pts, c.avg_pts, 0),
        2
    ) as second_half_pts_delta,
    round(
        coalesce(h.second_half_avg_min, c.avg_min, 0)
        - coalesce(h.first_half_avg_min, c.avg_min, 0),
        2
    ) as second_half_min_delta,
    round(
        coalesce(h.second_half_ts_pct, sp.season_ts_pct, 0)
        - coalesce(h.first_half_ts_pct, sp.season_ts_pct, 0),
        4
    ) as second_half_ts_delta,
    coalesce(c.avg_reb, 0) as season_avg_reb,
    coalesce(c.avg_ast, 0) as season_avg_ast,
    coalesce(c.avg_stl, 0) as season_avg_stl,
    coalesce(c.avg_blk, 0) as season_avg_blk,
    coalesce(c.avg_fg3m, 0) as season_avg_fg3m,
    coalesce(c.avg_tov, 0) as season_avg_tov,
    coalesce(c.avg_min, 0) as season_avg_min,
    coalesce(c.avg_fantasy_points_simple, 0) as season_avg_fantasy_points,
    coalesce(f.pts_last_10, f.pts_last_5, c.avg_pts, 0) as recent_pts,
    coalesce(f.reb_last_10, f.reb_last_5, c.avg_reb, 0) as recent_reb,
    coalesce(f.ast_last_10, f.ast_last_5, c.avg_ast, 0) as recent_ast,
    coalesce(f.stl_last_10, f.stl_last_5, c.avg_stl, 0) as recent_stl,
    coalesce(f.blk_last_10, f.blk_last_5, c.avg_blk, 0) as recent_blk,
    coalesce(f.fg3m_last_10, f.fg3m_last_5, c.avg_fg3m, 0) as recent_fg3m,
    coalesce(f.tov_last_10, f.tov_last_5, c.avg_tov, 0) as recent_tov,
    coalesce(f.avg_min_last_10, f.avg_min_last_5, c.avg_min, 0) as recent_min,
    coalesce(
        f.fantasy_points_last_10,
        f.fantasy_points_last_5,
        c.avg_fantasy_points_simple,
        0
    ) as recent_fantasy_points,
    coalesce(f.fantasy_points_delta_vs_season, 0) as fantasy_points_delta_vs_season,
    coalesce(f.minutes_delta_vs_season, 0) as minutes_delta_vs_season,
    coalesce(s.season_points_share_of_team, 0) as season_points_share_of_team,
    coalesce(
        s.recent_points_share_of_team,
        s.season_points_share_of_team,
        0
    ) as recent_points_share_of_team,
    coalesce(s.season_points_share_of_game, 0) as season_points_share_of_game,
    coalesce(
        s.recent_points_share_of_game,
        s.season_points_share_of_game,
        0
    ) as recent_points_share_of_game,
    coalesce(c.z_pts, 0) as z_pts,
    coalesce(c.z_reb, 0) as z_reb,
    coalesce(c.z_ast, 0) as z_ast,
    coalesce(c.z_stl, 0) as z_stl,
    coalesce(c.z_blk, 0) as z_blk,
    coalesce(c.z_fg3m, 0) as z_fg3m,
    coalesce(c.z_tov, 0) as z_tov,
    coalesce(c.z_min, 0) as z_min,
    coalesce(c.z_fantasy_points_simple, 0) as z_fantasy_points
from recent_form f
left join category_profile c
    on f.season = c.season
   and f.player_id = c.player_id
left join player_dimension d
    on f.player_id = d.player_id
   and f.season = d.latest_season
left join scoring_contribution s
    on f.season = s.season
   and f.player_id = s.player_id
left join style_profile sp
    on f.season = sp.season
   and f.player_id = sp.player_id
left join team_contribution_summary tc
    on f.season = tc.season
   and f.player_id = tc.player_id
   and coalesce(d.latest_team_abbr, c.latest_team_abbr, f.team_abbr) = tc.team_abbr
left join shot_location_profile sl
    on f.season = sl.season
   and f.player_id = sl.player_id
left join season_split_profile h
    on f.season = h.season
   and f.player_id = h.player_id
where coalesce(c.games_sampled, f.season_games, 0) >= 3
