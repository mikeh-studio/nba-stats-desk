{{ config(materialized='view', schema=env_var('BQ_DATASET_GOLD', env_var('BQ_DATASET', 'nba_gold'))) }}

-- No AM/PM is present in the stored game time. Use midnight UTC on game date
-- as a conservative cutoff, explicitly NOT the latest report before tipoff.
-- Reports can be retrospectively ingested; retain both report and ingest time.
with candidates as (
    select g.season, g.game_id, g.game_date, g.team_abbr, r.player_id,
        r.injury_status, r.reason, r.report_timestamp_utc,
        r.ingested_at_utc, r.source_url,
        min(r.injury_status) over (
            partition by g.season, g.game_id, g.team_abbr, r.player_id, r.report_timestamp_utc
        ) as min_status,
        max(r.injury_status) over (
            partition by g.season, g.game_id, g.team_abbr, r.player_id, r.report_timestamp_utc
        ) as max_status,
        row_number() over (
            partition by g.season, g.game_id, g.team_abbr, r.player_id
            order by r.report_timestamp_utc desc, r.ingested_at_utc desc, r.source_url desc
        ) as report_rank
    from {{ ref('team_game_context') }} g
    join {{ ref('stg_player_injury_reports_clean') }} r
        on g.season = r.season and g.game_date = r.game_date
        and g.team_abbr = r.team_abbr
        and r.matchup in (concat(g.team_abbr, '@', g.opponent_abbr), concat(g.opponent_abbr, '@', g.team_abbr))
    where r.player_id is not null
        and r.report_timestamp_utc < {{ context_day_start('g.game_date') }}
)
select season, game_id, game_date, team_abbr, player_id,
    case when min_status = max_status then injury_status else 'Unknown' end as injury_status,
    case when min_status = max_status then reason else null end as reason,
    report_timestamp_utc, ingested_at_utc, source_url,
    case when min_status = max_status then 0 else 1 end as conflicting_reports,
    'before_game_date_utc' as cutoff_policy
from candidates where report_rank = 1
