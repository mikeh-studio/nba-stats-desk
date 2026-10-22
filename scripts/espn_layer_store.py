"""Dev-only versioned publication of independently accepted ESPN layers."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from scripts.espn_corrections import LAYERS, changes, timestamp, validate
from scripts.espn_only_store import ESPNOnlyStore
from scripts.gamebook_cloud import ConfirmedQueryFailure
from scripts.gamebook_store import canonical, require


class ESPNLayerStore(ESPNOnlyStore):
    def setup(self):
        super().setup()
        definitions = {
            "espn_layer_games": "source_game_id STRING, season STRING, game_date DATE, home STRING, away STRING",
            "espn_layer_state": "source_game_id STRING, layer STRING, current_revision STRING, health STRING, last_observed_at TIMESTAMP, last_accepted_observed_at TIMESTAMP, last_changed_at TIMESTAMP, last_attempt_id STRING",
            "espn_layer_revisions": "source_game_id STRING, layer STRING, revision STRING, payload STRING, evidence_uri STRING, accepted_at TIMESTAMP",
            "espn_layer_attempts": "attempt_id STRING, source_game_id STRING, layer STRING, revision STRING, previous_revision STRING, status STRING, error STRING, evidence_uri STRING, observed_at TIMESTAMP, finished_at TIMESTAMP, changes STRING",
        }
        self.cloud.query(
            ";\n".join(
                f"CREATE TABLE IF NOT EXISTS `{self.prefix}.{name}` ({ddl})"
                for name, ddl in definitions.items()
            )
        )
        p = self.prefix
        views = {
            "espn_layer_current": f"SELECT s.*,r.payload,r.evidence_uri,a.status latest_attempt_status,a.error latest_error FROM `{p}.espn_layer_state` s LEFT JOIN `{p}.espn_layer_revisions` r USING(source_game_id,layer) LEFT JOIN `{p}.espn_layer_attempts` a ON s.last_attempt_id=a.attempt_id WHERE r.revision=s.current_revision OR (s.current_revision IS NULL AND r.revision IS NULL)",
        }
        views["espn_layer_status"] = f"""SELECT g.*,
r.current_revision result_revision,b.current_revision box_revision,p.current_revision pbp_revision,
CASE WHEN r.current_revision IS NULL THEN 'unavailable' WHEN r.health!='healthy' THEN 'retained_after_failure' ELSE 'accepted' END result_status,
CASE WHEN b.current_revision IS NULL THEN 'unavailable' WHEN JSON_VALUE(b.payload,'$.result_revision') IS DISTINCT FROM r.current_revision THEN 'stale' WHEN b.health!='healthy' OR r.health!='healthy' THEN 'retained_after_failure' ELSE 'accepted' END box_status,
CASE WHEN p.current_revision IS NULL THEN 'pending_correction' WHEN JSON_VALUE(p.payload,'$.result_revision') IS DISTINCT FROM r.current_revision OR JSON_VALUE(p.payload,'$.box_revision') IS DISTINCT FROM b.current_revision THEN 'stale' WHEN p.health!='healthy' OR b.health!='healthy' OR r.health!='healthy' THEN 'retained_after_failure' ELSE 'accepted' END pbp_status,
r.health result_health,b.health box_health,p.health pbp_health,
r.latest_error result_error,b.latest_error box_error,p.latest_error pbp_error,
LEAST(r.last_observed_at,b.last_observed_at,p.last_observed_at) last_checked_at,
GREATEST(r.last_changed_at,COALESCE(b.last_changed_at,r.last_changed_at),COALESCE(p.last_changed_at,r.last_changed_at)) last_changed_at,
CAST(JSON_VALUE(r.payload,'$.game.home_score') AS INT64) home_score,
CAST(JSON_VALUE(r.payload,'$.game.away_score') AS INT64) away_score,
CASE WHEN m.nba_game_id IS NULL THEN 'unresolved' WHEN JSON_VALUE(m.record,'$.Date')!=CAST(g.game_date AS STRING) OR JSON_VALUE(m.record,'$.HomeTeamAbbreviation')!=g.home OR JSON_VALUE(m.record,'$.AwayTeamAbbreviation')!=g.away OR JSON_VALUE(m.record,'$.HomePoints') IS DISTINCT FROM JSON_VALUE(r.payload,'$.game.home_score') OR JSON_VALUE(m.record,'$.AwayPoints') IS DISTINCT FROM JSON_VALUE(r.payload,'$.game.away_score') THEN 'conflict' ELSE 'resolved' END nba_game_mapping_status,m.nba_game_id,
IF(EXISTS(SELECT 1 FROM `{p}.espn_only_checks` q WHERE q.source_game_id=g.source_game_id AND q.revision=JSON_VALUE(p.payload,'$.legacy_revision') AND q.kind='pdf' AND q.status='matched'),'matched_for_pbp_revision','not_verified') pdf_verification_status
FROM `{p}.espn_layer_games` g
LEFT JOIN `{p}.espn_layer_current` r ON g.source_game_id=r.source_game_id AND r.layer='result'
LEFT JOIN `{p}.espn_layer_current` b ON g.source_game_id=b.source_game_id AND b.layer='box'
LEFT JOIN `{p}.espn_layer_current` p ON g.source_game_id=p.source_game_id AND p.layer='pbp'
LEFT JOIN `{p}.espn_mappings` m ON g.source_game_id=m.source_game_id"""
        views[
            "espn_layer_player_box_scores"
        ] = f"""SELECT s.source_game_id,s.current_revision revision,g.box_status,JSON_VALUE(player,'$.source_player_id') source_player_id,JSON_VALUE(player,'$.name') player_name,JSON_VALUE(player,'$.team') team,CAST(JSON_VALUE(player,'$.played') AS BOOL) played,JSON_QUERY(player,'$.stats') stats,m.nba_player_id
FROM `{p}.espn_layer_current` s JOIN `{p}.espn_layer_status` g USING(source_game_id),UNNEST(JSON_QUERY_ARRAY(s.payload,'$.players')) player LEFT JOIN `{p}.espn_player_mappings` m ON JSON_VALUE(player,'$.source_player_id')=m.source_player_id WHERE s.layer='box'"""
        views[
            "espn_layer_validated_assists"
        ] = f"""SELECT s.source_game_id,s.current_revision revision,g.result_revision,g.box_revision,JSON_VALUE(e,'$.source_event_id') source_event_id,JSON_VALUE(e,'$.team') team,JSON_VALUE(e,'$.assister_source_id') assister_source_id,JSON_VALUE(e,'$.scorer_source_id') scorer_source_id,CAST(JSON_VALUE(e,'$.period') AS INT64) period,CAST(JSON_VALUE(e,'$.clock_seconds') AS FLOAT64) clock_seconds,CAST(JSON_VALUE(e,'$.points') AS INT64) points,JSON_VALUE(e,'$.text') source_text,a.nba_player_id assister_nba_id,b.nba_player_id scorer_nba_id,g.nba_game_mapping_status,g.nba_game_id,g.pdf_verification_status
FROM `{p}.espn_layer_current` s JOIN `{p}.espn_layer_status` g USING(source_game_id),UNNEST(JSON_QUERY_ARRAY(s.payload,'$.assists')) e LEFT JOIN `{p}.espn_player_mappings` a ON JSON_VALUE(e,'$.assister_source_id')=a.source_player_id LEFT JOIN `{p}.espn_player_mappings` b ON JSON_VALUE(e,'$.scorer_source_id')=b.source_player_id WHERE s.layer='pbp' AND g.pbp_status='accepted'"""
        views["espn_layer_readiness"] = f"""WITH players AS (
SELECT source_game_id,COUNTIF(played) active_players,COUNTIF(played AND nba_player_id IS NOT NULL) mapped_players FROM `{p}.espn_layer_player_box_scores` GROUP BY 1), queue AS (
SELECT s.*,CASE WHEN last_checked_at IS NULL THEN TIMESTAMP(game_date,'America/New_York') ELSE
(SELECT MIN(TIMESTAMP(DATETIME(DATE_ADD(game_date,INTERVAL d DAY),TIME '12:00:00'),'America/New_York')) FROM UNNEST([1,2,3,4,5,6,7,14,21,28,30]) d WHERE (d NOT IN (21,28) OR result_status!='accepted' OR box_status!='accepted' OR pbp_status!='accepted') AND TIMESTAMP(DATETIME(DATE_ADD(game_date,INTERVAL d DAY),TIME '12:00:00'),'America/New_York')>last_checked_at) END next_check_at FROM `{p}.espn_layer_status` s)
SELECT q.*,COALESCE(p.active_players,0) active_players,COALESCE(p.mapped_players,0) mapped_players,
CASE WHEN p.active_players IS NULL THEN 'not_attempted' WHEN p.active_players=p.mapped_players THEN 'resolved' WHEN p.mapped_players>0 THEN 'partial' ELSE 'unresolved' END nba_player_mapping_status,
CASE WHEN DATE_DIFF(CURRENT_DATE('America/New_York'),q.game_date,DAY)>30 AND (next_check_at IS NOT NULL OR result_status!='accepted' OR box_status!='accepted' OR pbp_status!='accepted') THEN 'needs_review' WHEN next_check_at IS NULL THEN 'routine_checks_complete' WHEN next_check_at<=CURRENT_TIMESTAMP() THEN 'due' ELSE 'watching' END correction_status,
c.status latest_pdf_attempt_status,c.error latest_pdf_error
FROM queue q LEFT JOIN players p USING(source_game_id)
LEFT JOIN `{p}.espn_layer_current` b ON q.source_game_id=b.source_game_id AND b.layer='pbp'
LEFT JOIN `{p}.espn_only_latest_checks` c ON q.source_game_id=c.source_game_id AND JSON_VALUE(b.payload,'$.legacy_revision')=c.revision AND c.kind='pdf'"""
        from google.cloud import bigquery

        for name, query in views.items():
            table = bigquery.Table(f"{p}.{name}")
            table.view_query = query
            actual = self.cloud.bq.create_table(table, exists_ok=True, timeout=30)
            require(actual.view_query.strip() == query.strip(), f"View drift: {name}")

    def register_games(self, games):
        if not games:
            return
        self.cloud.query(
            f"""BEGIN TRANSACTION;
UPDATE `{self.prefix}.gamebook_store_control` SET generation=generation+1 WHERE store_id='main';
CREATE TEMP TABLE discovered AS SELECT JSON_VALUE(g,'$.source_game_id') source_game_id,JSON_VALUE(g,'$.season') season,CAST(JSON_VALUE(g,'$.date') AS DATE) game_date,JSON_VALUE(g,'$.home') home,JSON_VALUE(g,'$.away') away FROM UNNEST(JSON_QUERY_ARRAY(@games)) g;
ASSERT NOT EXISTS(SELECT source_game_id FROM discovered GROUP BY 1 HAVING COUNT(*)!=1) AS 'duplicate_game';
ASSERT NOT EXISTS(SELECT 1 FROM discovered d JOIN `{self.prefix}.espn_layer_games` g USING(source_game_id) WHERE d.season!=g.season OR d.game_date!=g.game_date OR d.home!=g.home OR d.away!=g.away) AS 'game_scope_changed';
INSERT INTO `{self.prefix}.espn_layer_games` SELECT * FROM discovered d WHERE NOT EXISTS(SELECT 1 FROM `{self.prefix}.espn_layer_games` g WHERE d.source_game_id=g.source_game_id);
INSERT INTO `{self.prefix}.espn_layer_state` SELECT d.source_game_id,l,NULL,'pending',NULL,NULL,NULL,NULL FROM discovered d CROSS JOIN UNNEST(['result','box','pbp']) l WHERE NOT EXISTS(SELECT 1 FROM `{self.prefix}.espn_layer_state` s WHERE s.source_game_id=d.source_game_id AND s.layer=l);
COMMIT TRANSACTION;""",
            [self.cloud.parameter("games", canonical(games))],
        )

    def coverage(self, season):
        rows = self.cloud.query(
            f"SELECT * FROM `{self.prefix}.espn_layer_readiness` WHERE season=@season ORDER BY game_date,source_game_id LIMIT 1501",
            [self.cloud.parameter("season", season)],
        )
        require(len(rows) <= 1500, "Coverage exceeds bound")
        return rows

    def failure(self, game, layer, error, evidence, observed_at=None, revision=None):
        require(layer in LAYERS, "Unknown layer")
        attempt = uuid4().hex
        observed = timestamp(observed_at).isoformat() if observed_at else None
        self.cloud.query(
            f"""BEGIN TRANSACTION;
UPDATE `{self.prefix}.gamebook_store_control` SET generation=generation+1 WHERE store_id='main';
INSERT INTO `{self.prefix}.espn_layer_attempts` SELECT @attempt,@id,@layer,@revision,current_revision,'rejected',@error,@evidence,@observed,CURRENT_TIMESTAMP(),'[]' FROM `{self.prefix}.espn_layer_state` WHERE source_game_id=@id AND layer=@layer;
UPDATE `{self.prefix}.espn_layer_state` SET health='failed',last_attempt_id=@attempt,last_observed_at=COALESCE(@observed,last_observed_at) WHERE source_game_id=@id AND layer=@layer AND (@observed IS NULL OR last_observed_at IS NULL OR @observed>=last_observed_at);
COMMIT TRANSACTION;""",
            self.params(
                {
                    "attempt": attempt,
                    "id": game["source_game_id"],
                    "layer": layer,
                    "revision": revision,
                    "error": error,
                    "evidence": evidence,
                    "observed": observed,
                }
            ),
        )
        return {
            "layer": layer,
            "status": "rejected",
            "error": error,
            "attempt_id": attempt,
        }

    def params(self, values):
        return [
            self.cloud.parameter(
                k,
                v,
                "TIMESTAMP"
                if k == "observed"
                else "BOOL"
                if k == "inject"
                else "STRING",
            )
            for k, v in values.items()
        ]

    def publish(self, candidate, evidence, observed_at, inject_failure=False):
        require(
            not inject_failure
            or self.cloud.dataset.startswith("nba_gamebook_dev_espn_test_"),
            "Fault injection only in synthetic test datasets",
        )
        game, layer = candidate["game"], candidate["layer"]
        observed = timestamp(observed_at)
        require(
            observed <= datetime.now(timezone.utc) + timedelta(minutes=5),
            "Future capture timestamp",
        )
        try:
            rev = validate(candidate)
        except (ValueError, KeyError, TypeError) as exc:
            return self.failure(game, layer, str(exc), evidence, observed)
        state = self.cloud.query(
            f"SELECT * FROM `{self.prefix}.espn_layer_current` WHERE source_game_id=@id AND layer=@layer",
            self.params({"id": game["source_game_id"], "layer": layer}),
        )
        require(len(state) == 1, "Layer not registered")
        previous = state[0]
        attempt = uuid4().hex
        body = canonical(candidate)
        uri = self.cloud.upload(body.encode(), "espn_layer_normalized", "json")
        params = self.params(
            {
                "id": game["source_game_id"],
                "layer": layer,
                "revision": rev,
                "expected": previous["current_revision"],
                "payload": body,
                "evidence": evidence,
                "attempt": attempt,
                "observed": observed.isoformat(),
                "inject": inject_failure,
                "diff": canonical(
                    changes(
                        json.loads(previous["payload"])
                        if previous["payload"]
                        else None,
                        candidate,
                    )
                ),
            }
        )
        try:
            rows = self.cloud.query(
                f"""DECLARE changed BOOL DEFAULT @revision IS DISTINCT FROM @expected;
BEGIN TRANSACTION;
UPDATE `{self.prefix}.gamebook_store_control` SET generation=generation+1 WHERE store_id='main';
ASSERT @@row_count=1 AS 'control_missing';
ASSERT (SELECT current_revision IS NOT DISTINCT FROM @expected FROM `{self.prefix}.espn_layer_state` WHERE source_game_id=@id AND layer=@layer) AS 'stale_layer_publisher';
ASSERT NOT EXISTS(SELECT 1 FROM `{self.prefix}.espn_layer_state` WHERE source_game_id=@id AND layer=@layer AND changed AND (last_observed_at>@observed OR last_accepted_observed_at=@observed)) AS 'older_or_conflicting_capture';
ASSERT EXISTS(SELECT 1 FROM `{self.prefix}.espn_layer_games` WHERE source_game_id=@id AND season=JSON_VALUE(@payload,'$.game.season') AND CAST(game_date AS STRING)=JSON_VALUE(@payload,'$.game.date') AND home=JSON_VALUE(@payload,'$.game.home') AND away=JSON_VALUE(@payload,'$.game.away')) AS 'candidate_game_scope';
ASSERT @layer='result' OR EXISTS(SELECT 1 FROM `{self.prefix}.espn_layer_state` WHERE source_game_id=@id AND layer='result' AND current_revision=JSON_VALUE(@payload,'$.result_revision') AND health='healthy') AS 'result_dependency_not_current';
ASSERT @layer!='pbp' OR EXISTS(SELECT 1 FROM `{self.prefix}.espn_layer_state` WHERE source_game_id=@id AND layer='box' AND current_revision=JSON_VALUE(@payload,'$.box_revision') AND health='healthy') AS 'box_dependency_not_current';
ASSERT NOT EXISTS(SELECT 1 FROM `{self.prefix}.espn_layer_revisions` WHERE source_game_id=@id AND layer=@layer AND revision=@revision AND payload!=@payload) AS 'revision_hash_collision';
INSERT INTO `{self.prefix}.espn_layer_revisions` SELECT @id,@layer,@revision,@payload,@evidence,CURRENT_TIMESTAMP() FROM UNNEST([1]) WHERE NOT EXISTS(SELECT 1 FROM `{self.prefix}.espn_layer_revisions` WHERE source_game_id=@id AND layer=@layer AND revision=@revision);
INSERT INTO `{self.prefix}.espn_layer_attempts` VALUES(@attempt,@id,@layer,@revision,@expected,IF(changed,'published','noop'),NULL,@evidence,@observed,CURRENT_TIMESTAMP(),@diff);
UPDATE `{self.prefix}.espn_layer_state` SET current_revision=@revision,health=IF(changed OR last_observed_at IS NULL OR @observed>last_observed_at,'healthy',health),last_changed_at=IF(changed,CURRENT_TIMESTAMP(),last_changed_at),last_accepted_observed_at=IF(last_accepted_observed_at IS NULL OR @observed>last_accepted_observed_at,@observed,last_accepted_observed_at),last_observed_at=IF(last_observed_at IS NULL OR @observed>last_observed_at,@observed,last_observed_at),last_attempt_id=IF(health='healthy' OR changed OR last_observed_at IS NULL OR @observed>last_observed_at,@attempt,last_attempt_id) WHERE source_game_id=@id AND layer=@layer;
ASSERT NOT @inject AS 'injected_layer_failure';
COMMIT TRANSACTION;
SELECT a.status,s.health FROM `{self.prefix}.espn_layer_attempts` a JOIN `{self.prefix}.espn_layer_state` s USING(source_game_id,layer) WHERE a.attempt_id=@attempt;""",
                params,
            )
            return {
                "layer": layer,
                "revision": rev,
                "attempt_id": attempt,
                "normalized_uri": uri,
                **rows[0],
            }
        except ConfirmedQueryFailure as exc:
            return self.failure(game, layer, str(exc), evidence, observed, rev)
