"""ESPN-reconciled dev facts with independent identity and PDF verification."""

from __future__ import annotations

from uuid import uuid4

from scripts.espn_payload import source_input  # noqa: F401
from scripts.espn_pbp_contract import VERSION, normalize, validate  # noqa: F401
from scripts.espn_store import ESPNStore
from scripts.gamebook_store import require


class ESPNOnlyStore(ESPNStore):
    source_only = True

    def __init__(self, cloud):
        require(
            cloud.dataset == "nba_gamebook_dev_espn_only"
            or cloud.dataset.startswith("nba_gamebook_dev_espn_test_"),
            "ESPN-only requires its isolated dev destination",
        )
        super().__init__(cloud)

    def validate_candidate(self, candidate):
        return validate(candidate)

    def publication_health(self, candidate):
        return "healthy"

    def setup(self):
        super().setup()
        self.cloud.query(
            f"CREATE TABLE IF NOT EXISTS `{self.prefix}.espn_only_checks` (attempt_id STRING,source_game_id STRING,revision STRING,kind STRING,status STRING,error STRING,evidence_uri STRING,finished_at TIMESTAMP)"
        )
        base = f"`{self.prefix}.espn_current_snapshots`"
        checks = f"`{self.prefix}.espn_only_checks`"
        views = {
            "espn_only_latest_checks": f"SELECT * FROM {checks} QUALIFY ROW_NUMBER() OVER(PARTITION BY source_game_id,revision,kind ORDER BY finished_at DESC,attempt_id DESC)=1",
            "espn_only_current_plays": f"SELECT source_game_id,revision,health,offset event_order,JSON_VALUE(p,'$.id') source_event_id,CAST(JSON_VALUE(p,'$.period.number') AS INT64) period,JSON_VALUE(p,'$.clock.displayValue') clock,JSON_VALUE(p,'$.text') source_text,JSON_VALUE(p,'$.team.id') source_team_id,CAST(JSON_VALUE(p,'$.homeScore') AS INT64) home_score,CAST(JSON_VALUE(p,'$.awayScore') AS INT64) away_score,CAST(JSON_VALUE(p,'$.scoringPlay') AS BOOL) scoring_play,CAST(JSON_VALUE(p,'$.scoreValue') AS INT64) score_value,JSON_QUERY_ARRAY(p,'$.participants') participants FROM {base}, UNNEST(JSON_QUERY_ARRAY(payload,'$.reconciliation_input.plays')) p WITH OFFSET AS offset",
        }
        views["espn_only_games"] = f"""WITH player_counts AS (
SELECT source_game_id,COUNTIF(played) active,COUNTIF(played AND nba_player_id IS NOT NULL) mapped
FROM `{self.prefix}.espn_resolved_player_box_scores` GROUP BY 1),
verified AS (SELECT source_game_id,revision,COUNTIF(kind='pdf' AND status='matched') matches FROM {checks} GROUP BY 1,2)
SELECT c.source_game_id,c.season,c.game_date,c.home,c.away,c.current_revision revision,c.health,
IF(s.revision IS NULL,'not_reconciled',JSON_VALUE(s.payload,'$.reconciliation_status')) reconciliation_status,
CASE WHEN m.nba_game_id IS NULL THEN 'unresolved'
WHEN JSON_VALUE(m.record,'$.Date')!=CAST(c.game_date AS STRING) OR JSON_VALUE(m.record,'$.HomeTeamAbbreviation')!=c.home OR JSON_VALUE(m.record,'$.AwayTeamAbbreviation')!=c.away OR JSON_VALUE(m.record,'$.HomePoints')!=JSON_VALUE(s.payload,'$.game.home_score') OR JSON_VALUE(m.record,'$.AwayPoints')!=JSON_VALUE(s.payload,'$.game.away_score') THEN 'conflict'
ELSE 'resolved' END nba_game_mapping_status,m.nba_game_id,
CASE WHEN p.active IS NULL THEN 'not_attempted' WHEN p.mapped=p.active THEN 'resolved' WHEN p.mapped>0 THEN 'partial' ELSE 'unresolved' END nba_player_mapping_status,
COALESCE(p.active,0) active_players,COALESCE(p.mapped,0) mapped_players,
IF(v.matches>0,'matched',COALESCE(q.status,'not_attempted')) pdf_verification_status,
q.status latest_pdf_attempt_status,q.error latest_pdf_error,
i.status latest_identity_attempt_status,i.error latest_identity_error,
g.status latest_game_mapping_attempt_status,g.error latest_game_mapping_error,
a.status latest_reconciliation_attempt_status,a.error latest_reconciliation_error
FROM `{self.prefix}.espn_coverage` c
LEFT JOIN {base} s ON c.source_game_id=s.source_game_id
LEFT JOIN `{self.prefix}.espn_mappings` m ON c.source_game_id=m.source_game_id
LEFT JOIN player_counts p ON c.source_game_id=p.source_game_id
LEFT JOIN verified v ON c.source_game_id=v.source_game_id AND c.current_revision=v.revision
LEFT JOIN `{self.prefix}.espn_only_latest_checks` q ON q.source_game_id=c.source_game_id AND q.revision=c.current_revision AND q.kind='pdf'
LEFT JOIN `{self.prefix}.espn_only_latest_checks` i ON i.source_game_id=c.source_game_id AND i.revision=c.current_revision AND i.kind='player_mapping'
LEFT JOIN `{self.prefix}.espn_only_latest_checks` g ON g.source_game_id=c.source_game_id AND g.revision=c.current_revision AND g.kind='game_mapping'
LEFT JOIN `{self.prefix}.espn_attempts` a ON a.attempt_id=c.last_attempt_id"""
        views[
            "espn_only_assists"
        ] = f"""SELECT e.* EXCEPT(nba_game_id,pdf_validation),g.nba_game_id,g.reconciliation_status,g.nba_game_mapping_status,g.nba_player_mapping_status,g.pdf_verification_status
FROM `{self.prefix}.espn_resolved_assists` e JOIN `{self.prefix}.espn_only_games` g USING(source_game_id,revision)"""
        from google.cloud import bigquery

        for name, query in views.items():
            table = bigquery.Table(f"{self.prefix}.{name}")
            table.view_query = query
            actual = self.cloud.bq.create_table(table, exists_ok=True, timeout=30)
            require(actual.view_query.strip() == query.strip(), f"View drift: {name}")

    def record_check(self, candidate, kind, status, evidence, error=None):
        allowed = {
            "pdf": {"matched", "unavailable", "mismatch"},
            "player_mapping": {"resolved", "partial", "failed"},
            "game_mapping": {"resolved", "unresolved", "failed"},
        }
        require(kind in allowed and status in allowed[kind], "Invalid check status")
        revision = validate(candidate)
        self.cloud.query(
            f"""BEGIN TRANSACTION;
UPDATE `{self.prefix}.gamebook_store_control` SET generation=generation+1 WHERE store_id='main';
ASSERT (SELECT COUNT(*)=1 FROM `{self.prefix}.espn_revisions` WHERE source_game_id=@id AND revision=@revision) AS 'check_revision_missing';
INSERT INTO `{self.prefix}.espn_only_checks` VALUES(@attempt,@id,@revision,@kind,@status,@error,@evidence,CURRENT_TIMESTAMP());
COMMIT TRANSACTION;""",
            [
                self.cloud.parameter(k, v)
                for k, v in {
                    "attempt": uuid4().hex,
                    "id": candidate["game"]["source_game_id"],
                    "revision": revision,
                    "kind": kind,
                    "status": status,
                    "error": error,
                    "evidence": evidence,
                }.items()
            ],
        )
