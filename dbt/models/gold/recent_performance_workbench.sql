{{ config(
    materialized='table',
    schema=env_var('BQ_DATASET_GOLD', env_var('BQ_DATASET', 'nba_gold')),
    cluster_by=['season', 'game_date', 'game_id']
) }}

with playoff_games as (
    select distinct
        stats.season,
        stats.season_type,
        stats.game_id,
        stats.game_date
    from {{ ref('fct_player_game_stats') }} stats
    where stats.season = '2025-26'
      and stats.season_type = 'Playoffs'
      and coalesce(cast(stats.min as {{ float64_type() }}), 0) >= 1
),
game_rollups as (
    select
        playoff_games.season,
        playoff_games.season_type,
        playoff_games.game_id,
        playoff_games.game_date,
        string_agg(distinct stats.team_abbr, ' / ' order by stats.team_abbr) as teams,
        min(stats.matchup) as matchup,
        dim_game.home_team_abbr,
        dim_game.away_team_abbr,
        dim_game.home_team_pts,
        dim_game.away_team_pts,
        count(distinct stats.player_id) as players_played
    from playoff_games
    join {{ ref('fct_player_game_stats') }} stats
      on stats.season = playoff_games.season
     and stats.game_id = playoff_games.game_id
     and stats.season_type = 'Playoffs'
     and coalesce(cast(stats.min as {{ float64_type() }}), 0) >= 1
    left join {{ ref('dim_game') }} dim_game
      on dim_game.season = playoff_games.season
     and dim_game.game_id = playoff_games.game_id
    group by
        playoff_games.season,
        playoff_games.season_type,
        playoff_games.game_id,
        playoff_games.game_date,
        dim_game.home_team_abbr,
        dim_game.away_team_abbr,
        dim_game.home_team_pts,
        dim_game.away_team_pts
),
selected_players as (
    select
        s.season,
        s.season_type,
        s.game_id,
        s.game_date,
        cast(s.player_id as {{ int64_type() }}) as player_id,
        s.player_name,
        s.team_abbr,
        s.opponent_abbr,
        s.home_away,
        s.matchup,
        s.wl,
        s.min,
        s.pts,
        s.reb,
        s.ast,
        s.stl,
        s.blk,
        s.fg_pct,
        s.ft_pct,
        s.fg3m
    from {{ ref('fct_player_game_stats') }} s
    join playoff_games
      on playoff_games.season = s.season
     and playoff_games.game_id = s.game_id
    where s.season_type = 'Playoffs'
      and coalesce(cast(s.min as {{ float64_type() }}), 0) >= 1
),
selected_player_ids as (
    select distinct season, player_id
    from selected_players
    where player_id is not null
),
baseline as (
    select
        stats.season,
        cast(stats.player_id as {{ int64_type() }}) as player_id,
        selected_players.game_id,
        selected_players.game_date,
        count(*) as games_sampled,
        avg(stats.pts) as avg_pts,
        avg(stats.reb) as avg_reb,
        avg(stats.ast) as avg_ast,
        avg(stats.stl) as avg_stl,
        avg(stats.blk) as avg_blk,
        avg(stats.min) as avg_min,
        avg(stats.fg_pct) as avg_fg_pct,
        avg(stats.ft_pct) as avg_ft_pct,
        avg(stats.fg3m) as avg_fg3m,
        stddev_pop(stats.pts) as sd_pts,
        stddev_pop(stats.reb) as sd_reb,
        stddev_pop(stats.ast) as sd_ast,
        stddev_pop(stats.stl) as sd_stl,
        stddev_pop(stats.blk) as sd_blk,
        approx_quantiles(stats.pts, 100)[offset(10)] as pts_p10,
        approx_quantiles(stats.pts, 100)[offset(25)] as pts_p25,
        approx_quantiles(stats.pts, 100)[offset(50)] as pts_p50,
        approx_quantiles(stats.pts, 100)[offset(75)] as pts_p75,
        approx_quantiles(stats.pts, 100)[offset(90)] as pts_p90,
        round(
            {{ safe_divide(
                'countif(stats.pts < selected_players.pts) + 0.5 * countif(stats.pts = selected_players.pts)',
                'count(*)'
            ) }} * 100,
            1
        ) as pts_percentile,
        approx_quantiles(stats.reb, 100)[offset(10)] as reb_p10,
        approx_quantiles(stats.reb, 100)[offset(25)] as reb_p25,
        approx_quantiles(stats.reb, 100)[offset(50)] as reb_p50,
        approx_quantiles(stats.reb, 100)[offset(75)] as reb_p75,
        approx_quantiles(stats.reb, 100)[offset(90)] as reb_p90,
        round(
            {{ safe_divide(
                'countif(stats.reb < selected_players.reb) + 0.5 * countif(stats.reb = selected_players.reb)',
                'count(*)'
            ) }} * 100,
            1
        ) as reb_percentile,
        approx_quantiles(stats.ast, 100)[offset(10)] as ast_p10,
        approx_quantiles(stats.ast, 100)[offset(25)] as ast_p25,
        approx_quantiles(stats.ast, 100)[offset(50)] as ast_p50,
        approx_quantiles(stats.ast, 100)[offset(75)] as ast_p75,
        approx_quantiles(stats.ast, 100)[offset(90)] as ast_p90,
        round(
            {{ safe_divide(
                'countif(stats.ast < selected_players.ast) + 0.5 * countif(stats.ast = selected_players.ast)',
                'count(*)'
            ) }} * 100,
            1
        ) as ast_percentile,
        approx_quantiles(stats.stl, 100)[offset(10)] as stl_p10,
        approx_quantiles(stats.stl, 100)[offset(25)] as stl_p25,
        approx_quantiles(stats.stl, 100)[offset(50)] as stl_p50,
        approx_quantiles(stats.stl, 100)[offset(75)] as stl_p75,
        approx_quantiles(stats.stl, 100)[offset(90)] as stl_p90,
        round(
            {{ safe_divide(
                'countif(stats.stl < selected_players.stl) + 0.5 * countif(stats.stl = selected_players.stl)',
                'count(*)'
            ) }} * 100,
            1
        ) as stl_percentile,
        approx_quantiles(stats.blk, 100)[offset(10)] as blk_p10,
        approx_quantiles(stats.blk, 100)[offset(25)] as blk_p25,
        approx_quantiles(stats.blk, 100)[offset(50)] as blk_p50,
        approx_quantiles(stats.blk, 100)[offset(75)] as blk_p75,
        approx_quantiles(stats.blk, 100)[offset(90)] as blk_p90,
        round(
            {{ safe_divide(
                'countif(stats.blk < selected_players.blk) + 0.5 * countif(stats.blk = selected_players.blk)',
                'count(*)'
            ) }} * 100,
            1
        ) as blk_percentile,
        approx_quantiles(stats.min, 100)[offset(10)] as min_p10,
        approx_quantiles(stats.min, 100)[offset(25)] as min_p25,
        approx_quantiles(stats.min, 100)[offset(50)] as min_p50,
        approx_quantiles(stats.min, 100)[offset(75)] as min_p75,
        approx_quantiles(stats.min, 100)[offset(90)] as min_p90,
        round(
            {{ safe_divide(
                'countif(stats.min < selected_players.min) + 0.5 * countif(stats.min = selected_players.min)',
                'count(*)'
            ) }} * 100,
            1
        ) as min_percentile,
        approx_quantiles(stats.fg_pct, 100)[offset(10)] as fg_pct_p10,
        approx_quantiles(stats.fg_pct, 100)[offset(25)] as fg_pct_p25,
        approx_quantiles(stats.fg_pct, 100)[offset(50)] as fg_pct_p50,
        approx_quantiles(stats.fg_pct, 100)[offset(75)] as fg_pct_p75,
        approx_quantiles(stats.fg_pct, 100)[offset(90)] as fg_pct_p90,
        case
            when selected_players.fg_pct is null then null
            else round(
                {{ safe_divide(
                    'countif(stats.fg_pct < selected_players.fg_pct) + 0.5 * countif(stats.fg_pct = selected_players.fg_pct)',
                    'nullif(countif(stats.fg_pct is not null), 0)'
                ) }} * 100,
                1
            )
        end as fg_pct_percentile,
        approx_quantiles(stats.ft_pct, 100)[offset(10)] as ft_pct_p10,
        approx_quantiles(stats.ft_pct, 100)[offset(25)] as ft_pct_p25,
        approx_quantiles(stats.ft_pct, 100)[offset(50)] as ft_pct_p50,
        approx_quantiles(stats.ft_pct, 100)[offset(75)] as ft_pct_p75,
        approx_quantiles(stats.ft_pct, 100)[offset(90)] as ft_pct_p90,
        case
            when selected_players.ft_pct is null then null
            else round(
                {{ safe_divide(
                    'countif(stats.ft_pct < selected_players.ft_pct) + 0.5 * countif(stats.ft_pct = selected_players.ft_pct)',
                    'nullif(countif(stats.ft_pct is not null), 0)'
                ) }} * 100,
                1
            )
        end as ft_pct_percentile,
        approx_quantiles(stats.fg3m, 100)[offset(10)] as fg3m_p10,
        approx_quantiles(stats.fg3m, 100)[offset(25)] as fg3m_p25,
        approx_quantiles(stats.fg3m, 100)[offset(50)] as fg3m_p50,
        approx_quantiles(stats.fg3m, 100)[offset(75)] as fg3m_p75,
        approx_quantiles(stats.fg3m, 100)[offset(90)] as fg3m_p90,
        round(
            {{ safe_divide(
                'countif(stats.fg3m < selected_players.fg3m) + 0.5 * countif(stats.fg3m = selected_players.fg3m)',
                'count(*)'
            ) }} * 100,
            1
        ) as fg3m_percentile
    from {{ ref('fct_player_game_stats') }} stats
    join selected_player_ids
      on selected_player_ids.season = stats.season
     and selected_player_ids.player_id = cast(stats.player_id as {{ int64_type() }})
    join selected_players
      on selected_players.season = stats.season
     and selected_players.player_id = cast(stats.player_id as {{ int64_type() }})
     and selected_players.game_date >= stats.game_date
    where coalesce(cast(stats.min as {{ float64_type() }}), 0) >= 1
    group by 1, 2, 3, 4
),
metric_rows as (
    select
        selected_players.*,
        baseline.games_sampled,
        baseline.avg_pts,
        baseline.avg_reb,
        baseline.avg_ast,
        baseline.avg_stl,
        baseline.avg_blk,
        baseline.avg_min,
        baseline.avg_fg_pct,
        baseline.avg_ft_pct,
        baseline.avg_fg3m,
        baseline.pts_p10,
        baseline.pts_p25,
        baseline.pts_p50,
        baseline.pts_p75,
        baseline.pts_p90,
        baseline.pts_percentile,
        baseline.reb_p10,
        baseline.reb_p25,
        baseline.reb_p50,
        baseline.reb_p75,
        baseline.reb_p90,
        baseline.reb_percentile,
        baseline.ast_p10,
        baseline.ast_p25,
        baseline.ast_p50,
        baseline.ast_p75,
        baseline.ast_p90,
        baseline.ast_percentile,
        baseline.stl_p10,
        baseline.stl_p25,
        baseline.stl_p50,
        baseline.stl_p75,
        baseline.stl_p90,
        baseline.stl_percentile,
        baseline.blk_p10,
        baseline.blk_p25,
        baseline.blk_p50,
        baseline.blk_p75,
        baseline.blk_p90,
        baseline.blk_percentile,
        baseline.min_p10,
        baseline.min_p25,
        baseline.min_p50,
        baseline.min_p75,
        baseline.min_p90,
        baseline.min_percentile,
        baseline.fg_pct_p10,
        baseline.fg_pct_p25,
        baseline.fg_pct_p50,
        baseline.fg_pct_p75,
        baseline.fg_pct_p90,
        baseline.fg_pct_percentile,
        baseline.ft_pct_p10,
        baseline.ft_pct_p25,
        baseline.ft_pct_p50,
        baseline.ft_pct_p75,
        baseline.ft_pct_p90,
        baseline.ft_pct_percentile,
        baseline.fg3m_p10,
        baseline.fg3m_p25,
        baseline.fg3m_p50,
        baseline.fg3m_p75,
        baseline.fg3m_p90,
        baseline.fg3m_percentile,
        round(selected_players.pts - baseline.avg_pts, 1) as pts_delta,
        round(selected_players.reb - baseline.avg_reb, 1) as reb_delta,
        round(selected_players.ast - baseline.avg_ast, 1) as ast_delta,
        round(selected_players.stl - baseline.avg_stl, 1) as stl_delta,
        round(selected_players.blk - baseline.avg_blk, 1) as blk_delta,
        round(selected_players.min - baseline.avg_min, 1) as min_delta,
        round(selected_players.fg_pct - baseline.avg_fg_pct, 3) as fg_pct_delta,
        round(selected_players.ft_pct - baseline.avg_ft_pct, 3) as ft_pct_delta,
        round(selected_players.fg3m - baseline.avg_fg3m, 1) as fg3m_delta,
        round({{ safe_divide('selected_players.pts - baseline.avg_pts', 'nullif(baseline.avg_pts, 0)') }} * 100, 1) as pts_delta_pct,
        round({{ safe_divide('selected_players.reb - baseline.avg_reb', 'nullif(baseline.avg_reb, 0)') }} * 100, 1) as reb_delta_pct,
        round({{ safe_divide('selected_players.ast - baseline.avg_ast', 'nullif(baseline.avg_ast, 0)') }} * 100, 1) as ast_delta_pct,
        round({{ safe_divide('selected_players.stl - baseline.avg_stl', 'nullif(baseline.avg_stl, 0)') }} * 100, 1) as stl_delta_pct,
        round({{ safe_divide('selected_players.blk - baseline.avg_blk', 'nullif(baseline.avg_blk, 0)') }} * 100, 1) as blk_delta_pct,
        round({{ safe_divide('selected_players.min - baseline.avg_min', 'nullif(baseline.avg_min, 0)') }} * 100, 1) as min_delta_pct,
        round({{ safe_divide('selected_players.fg_pct - baseline.avg_fg_pct', 'nullif(baseline.avg_fg_pct, 0)') }} * 100, 1) as fg_pct_delta_pct,
        round({{ safe_divide('selected_players.ft_pct - baseline.avg_ft_pct', 'nullif(baseline.avg_ft_pct, 0)') }} * 100, 1) as ft_pct_delta_pct,
        round({{ safe_divide('selected_players.fg3m - baseline.avg_fg3m', 'nullif(baseline.avg_fg3m, 0)') }} * 100, 1) as fg3m_delta_pct,
        case
            when baseline.sd_pts > 0 then {{ safe_divide('selected_players.pts - baseline.avg_pts', 'baseline.sd_pts') }}
            when selected_players.pts > baseline.avg_pts then 1.0
            when selected_players.pts < baseline.avg_pts then -1.0
            else 0.0
        end as z_pts,
        case
            when baseline.sd_reb > 0 then {{ safe_divide('selected_players.reb - baseline.avg_reb', 'baseline.sd_reb') }}
            when selected_players.reb > baseline.avg_reb then 1.0
            when selected_players.reb < baseline.avg_reb then -1.0
            else 0.0
        end as z_reb,
        case
            when baseline.sd_ast > 0 then {{ safe_divide('selected_players.ast - baseline.avg_ast', 'baseline.sd_ast') }}
            when selected_players.ast > baseline.avg_ast then 1.0
            when selected_players.ast < baseline.avg_ast then -1.0
            else 0.0
        end as z_ast,
        case
            when baseline.sd_stl > 0 then {{ safe_divide('selected_players.stl - baseline.avg_stl', 'baseline.sd_stl') }}
            when selected_players.stl > baseline.avg_stl then 1.0
            when selected_players.stl < baseline.avg_stl then -1.0
            else 0.0
        end as z_stl,
        case
            when baseline.sd_blk > 0 then {{ safe_divide('selected_players.blk - baseline.avg_blk', 'baseline.sd_blk') }}
            when selected_players.blk > baseline.avg_blk then 1.0
            when selected_players.blk < baseline.avg_blk then -1.0
            else 0.0
        end as z_blk,
        (
            case when selected_players.pts > baseline.avg_pts then 1 else 0 end
            + case when selected_players.reb > baseline.avg_reb then 1 else 0 end
            + case when selected_players.ast > baseline.avg_ast then 1 else 0 end
            + case when selected_players.stl > baseline.avg_stl then 1 else 0 end
            + case when selected_players.blk > baseline.avg_blk then 1 else 0 end
        ) as above_count,
        (
            case when selected_players.pts < baseline.avg_pts then 1 else 0 end
            + case when selected_players.reb < baseline.avg_reb then 1 else 0 end
            + case when selected_players.ast < baseline.avg_ast then 1 else 0 end
            + case when selected_players.stl < baseline.avg_stl then 1 else 0 end
            + case when selected_players.blk < baseline.avg_blk then 1 else 0 end
        ) as below_count
    from selected_players
    join baseline
      on baseline.season = selected_players.season
     and baseline.player_id = selected_players.player_id
     and baseline.game_id = selected_players.game_id
),
scored as (
    select
        *,
        round(z_pts + z_reb + z_ast + z_stl + z_blk, 2) as performance_score
    from metric_rows
),
ranked as (
    select
        scored.*,
        case
            when performance_score >= 1.0 or (performance_score > 0 and above_count >= 3) then 'above'
            when performance_score <= -1.0 or (performance_score < 0 and below_count >= 3) then 'below'
            else 'near'
        end as performance_status,
        row_number() over (
            partition by season, game_date
            order by abs(performance_score) desc, above_count desc, player_name
        ) as date_rank,
        row_number() over (
            partition by season, game_id
            order by abs(performance_score) desc, above_count desc, player_name
        ) as game_rank
    from scored
),
trend_payloads as (
    select
        selected_players.season,
        selected_players.game_id,
        selected_players.player_id,
        to_json_string(
            array_agg(
                struct(
                    stats.game_id,
                    stats.game_date,
                    stats.matchup,
                    stats.min,
                    stats.pts,
                    stats.reb,
                    stats.ast,
                    stats.stl,
                    stats.blk,
                    stats.fg_pct,
                    stats.ft_pct,
                    stats.fg3m
                )
                order by stats.game_date, stats.game_id
            )
        ) as trend_points_json
    from selected_players
    join {{ ref('fct_player_game_stats') }} stats
      on stats.season = selected_players.season
     and cast(stats.player_id as {{ int64_type() }}) = selected_players.player_id
     and stats.game_date between date_sub(selected_players.game_date, interval 29 day)
                             and selected_players.game_date
    where coalesce(cast(stats.min as {{ float64_type() }}), 0) >= 1
    group by 1, 2, 3
)
select
    ranked.season,
    ranked.season_type,
    ranked.game_id,
    ranked.game_date,
    game_rollups.teams,
    game_rollups.matchup as game_matchup,
    game_rollups.home_team_abbr,
    game_rollups.away_team_abbr,
    game_rollups.home_team_pts,
    game_rollups.away_team_pts,
    game_rollups.players_played,
    ranked.player_id,
    ranked.player_name,
    ranked.team_abbr,
    ranked.opponent_abbr,
    ranked.home_away,
    ranked.matchup,
    ranked.wl,
    ranked.min,
    ranked.pts,
    ranked.reb,
    ranked.ast,
    ranked.stl,
    ranked.blk,
    ranked.fg_pct,
    ranked.ft_pct,
    ranked.fg3m,
    ranked.games_sampled,
    ranked.avg_pts,
    ranked.avg_reb,
    ranked.avg_ast,
    ranked.avg_stl,
    ranked.avg_blk,
    ranked.avg_min,
    ranked.avg_fg_pct,
    ranked.avg_ft_pct,
    ranked.avg_fg3m,
    ranked.pts_p10,
    ranked.pts_p25,
    ranked.pts_p50,
    ranked.pts_p75,
    ranked.pts_p90,
    ranked.pts_percentile,
    ranked.reb_p10,
    ranked.reb_p25,
    ranked.reb_p50,
    ranked.reb_p75,
    ranked.reb_p90,
    ranked.reb_percentile,
    ranked.ast_p10,
    ranked.ast_p25,
    ranked.ast_p50,
    ranked.ast_p75,
    ranked.ast_p90,
    ranked.ast_percentile,
    ranked.stl_p10,
    ranked.stl_p25,
    ranked.stl_p50,
    ranked.stl_p75,
    ranked.stl_p90,
    ranked.stl_percentile,
    ranked.blk_p10,
    ranked.blk_p25,
    ranked.blk_p50,
    ranked.blk_p75,
    ranked.blk_p90,
    ranked.blk_percentile,
    ranked.min_p10,
    ranked.min_p25,
    ranked.min_p50,
    ranked.min_p75,
    ranked.min_p90,
    ranked.min_percentile,
    ranked.fg_pct_p10,
    ranked.fg_pct_p25,
    ranked.fg_pct_p50,
    ranked.fg_pct_p75,
    ranked.fg_pct_p90,
    ranked.fg_pct_percentile,
    ranked.ft_pct_p10,
    ranked.ft_pct_p25,
    ranked.ft_pct_p50,
    ranked.ft_pct_p75,
    ranked.ft_pct_p90,
    ranked.ft_pct_percentile,
    ranked.fg3m_p10,
    ranked.fg3m_p25,
    ranked.fg3m_p50,
    ranked.fg3m_p75,
    ranked.fg3m_p90,
    ranked.fg3m_percentile,
    ranked.pts_delta,
    ranked.reb_delta,
    ranked.ast_delta,
    ranked.stl_delta,
    ranked.blk_delta,
    ranked.min_delta,
    ranked.fg_pct_delta,
    ranked.ft_pct_delta,
    ranked.fg3m_delta,
    ranked.pts_delta_pct,
    ranked.reb_delta_pct,
    ranked.ast_delta_pct,
    ranked.stl_delta_pct,
    ranked.blk_delta_pct,
    ranked.min_delta_pct,
    ranked.fg_pct_delta_pct,
    ranked.ft_pct_delta_pct,
    ranked.fg3m_delta_pct,
    ranked.performance_score,
    ranked.performance_status,
    ranked.above_count,
    ranked.below_count,
    ranked.date_rank,
    ranked.game_rank,
    trend_payloads.trend_points_json
from ranked
join game_rollups
  on game_rollups.season = ranked.season
 and game_rollups.game_id = ranked.game_id
left join trend_payloads
  on trend_payloads.season = ranked.season
 and trend_payloads.game_id = ranked.game_id
 and trend_payloads.player_id = ranked.player_id
