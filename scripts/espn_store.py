"""Revisioned ESPN snapshots in isolated dev BigQuery, with atomic current views."""

from __future__ import annotations

from collections import Counter
from uuid import uuid4

from scripts.gamebook_cloud import ConfirmedQueryFailure
from scripts.gamebook_store import canonical, require
from scripts.parse_gamebooks import sha


def validate(candidate):
    g = candidate["game"]
    require(
        candidate["schema_version"] == "espn-dev/1"
        and g["provider"] == "espn"
        and g["final"],
        "Invalid candidate scope",
    )
    players = candidate["players"]
    edges = candidate["assists"]
    ids = {p["source_player_id"]: p for p in players}
    require(len(ids) == len(players), "Duplicate player")
    require(
        len({e["source_event_id"] for e in edges}) == len(edges), "Duplicate assist"
    )
    totals = Counter()
    for e in edges:
        a = ids[e["assister_source_id"]]
        s = ids[e["scorer_source_id"]]
        require(
            a["played"]
            and s["played"]
            and a["team"] == s["team"] == e["team"]
            and a != s,
            "Invalid assist relationship",
        )
        require(
            a["nba_player_id"] == e["assister_nba_id"]
            and s["nba_player_id"] == e["scorer_nba_id"],
            "Mapped player differs",
        )
        totals[a["source_player_id"]] += 1
    for p in players:
        if p["played"]:
            st = p["stats"]
            require(st["AST"] == totals[p["source_player_id"]], "Assist recount failed")
            require(
                st["PTS"] == 2 * st["FGM"] + st["FG3M"] + st["FTM"],
                "Scoring recount failed",
            )
    for side in ("home", "away"):
        require(
            sum(
                p["stats"]["PTS"]
                for p in players
                if p["played"] and p["team"] == g[side]
            )
            == g[side + "_score"],
            "Team scoring recount failed",
        )
    status = candidate["pdf_validation"]["status"]
    require(status in ("matched", "pending_mapping"), "Candidate PDF validation failed")
    if status == "matched":
        require(
            candidate["nba_game_id"]
            and all(p["nba_player_id"] for p in players if p["played"]),
            "Missing canonical IDs",
        )
        require(
            Counter(
                m["source_event_id"] for m in candidate["pdf_validation"]["matches"]
            )
            == Counter(e["source_event_id"] for e in edges),
            "PDF matches incomplete",
        )
    return sha(canonical(candidate).encode())


class ESPNStore:
    source_only = False

    def validate_candidate(self, candidate):
        return validate(candidate)

    def publication_health(self, candidate):
        return (
            "healthy"
            if candidate["pdf_validation"]["status"] == "matched"
            else "pending_mapping"
        )

    def __init__(self, cloud):
        require(
            cloud.dataset.startswith("nba_gamebook_dev_espn"),
            "ESPN requires its own dev dataset prefix",
        )
        self.cloud = cloud
        self.prefix = cloud.destination

    def setup(self):
        # Reuse the established dev-only dataset/IAM/schema checks and uploader.
        self.cloud.setup()
        definitions = {
            "espn_identity_attempts": "attempt_id STRING, status STRING, inserted INT64, error STRING, evidence_uri STRING, finished_at TIMESTAMP",
            "espn_player_mappings": "source_player_id STRING, nba_player_id STRING, normalized_name STRING, method STRING, catalog_sha256 STRING, evidence_uri STRING",
            "espn_mappings": "source_game_id STRING, nba_game_id STRING, record STRING, evidence_uri STRING",
            "espn_coverage": "source_game_id STRING, season STRING, game_date DATE, home STRING, away STRING, current_revision STRING, health STRING, last_attempt_id STRING",
            "espn_revisions": "source_game_id STRING, revision STRING, payload STRING, evidence_uri STRING, accepted_at TIMESTAMP",
            "espn_attempts": "attempt_id STRING, source_game_id STRING, revision STRING, status STRING, error STRING, evidence_uri STRING, finished_at TIMESTAMP",
        }
        self.cloud.query(
            ";\n".join(
                f"CREATE TABLE IF NOT EXISTS `{self.prefix}.{t}` ({ddl})"
                for t, ddl in definitions.items()
            )
        )
        # Views are read-only projections of immutable normalized snapshots.
        base = f"`{self.prefix}.espn_current_snapshots`"
        views = {
            "espn_current_snapshots": f"SELECT r.*, c.health FROM `{self.prefix}.espn_revisions` r JOIN `{self.prefix}.espn_coverage` c USING(source_game_id) WHERE r.revision=c.current_revision",
            "espn_current_games": f"SELECT source_game_id,revision,health,JSON_VALUE(payload,'$.nba_game_id') nba_game_id,JSON_VALUE(payload,'$.game.season') season,CAST(JSON_VALUE(payload,'$.game.date') AS DATE) game_date,JSON_VALUE(payload,'$.game.home') home,JSON_VALUE(payload,'$.game.away') away,CAST(JSON_VALUE(payload,'$.game.home_score') AS INT64) home_score,CAST(JSON_VALUE(payload,'$.game.away_score') AS INT64) away_score,JSON_VALUE(payload,'$.pdf_validation.status') pdf_validation FROM {base}",
            "espn_current_player_box_scores": f"SELECT source_game_id,revision,health,JSON_VALUE(p,'$.source_player_id') source_player_id,JSON_VALUE(p,'$.nba_player_id') nba_player_id,JSON_VALUE(p,'$.name') player_name,JSON_VALUE(p,'$.team') team,CAST(JSON_VALUE(p,'$.played') AS BOOL) played,CAST(JSON_VALUE(p,'$.stats.PTS') AS INT64) points,CAST(JSON_VALUE(p,'$.stats.AST') AS INT64) assists,CAST(JSON_VALUE(p,'$.stats.REB') AS INT64) rebounds,CAST(JSON_VALUE(p,'$.stats.STL') AS INT64) steals,CAST(JSON_VALUE(p,'$.stats.BLK') AS INT64) blocks,JSON_QUERY(p,'$.stats') stats FROM {base},UNNEST(JSON_QUERY_ARRAY(payload,'$.players')) p",
            "espn_current_assists": f"SELECT source_game_id,revision,health,JSON_VALUE(payload,'$.nba_game_id') nba_game_id,JSON_VALUE(payload,'$.pdf_validation.status') pdf_validation,JSON_VALUE(e,'$.source_event_id') source_event_id,JSON_VALUE(e,'$.assister_source_id') assister_source_id,JSON_VALUE(e,'$.scorer_source_id') scorer_source_id,JSON_VALUE(e,'$.assister_nba_id') assister_nba_id,JSON_VALUE(e,'$.scorer_nba_id') scorer_nba_id,CAST(JSON_VALUE(e,'$.period') AS INT64) period,CAST(JSON_VALUE(e,'$.clock_seconds') AS FLOAT64) clock_seconds,JSON_VALUE(e,'$.team') team,CAST(JSON_VALUE(e,'$.points') AS INT64) points,JSON_VALUE(e,'$.text') source_text FROM {base},UNNEST(JSON_QUERY_ARRAY(payload,'$.assists')) e",
            "espn_current_pdf_matches": f"SELECT source_game_id,revision,JSON_VALUE(m,'$.source_event_id') source_event_id,JSON_VALUE(m,'$.pdf_event_id') pdf_event_id,CAST(JSON_VALUE(m,'$.page') AS INT64) pdf_page,JSON_VALUE(m,'$.url') pdf_url FROM {base},UNNEST(JSON_QUERY_ARRAY(payload,'$.pdf_validation.matches')) m",
        }
        views.update(
            {
                "espn_resolved_player_box_scores": f"SELECT p.* EXCEPT(nba_player_id), COALESCE(p.nba_player_id,m.nba_player_id) nba_player_id,m.method identity_method,m.catalog_sha256,m.evidence_uri identity_evidence_uri FROM `{self.prefix}.espn_current_player_box_scores` p LEFT JOIN `{self.prefix}.espn_player_mappings` m USING(source_player_id)",
                "espn_resolved_assists": f"SELECT e.* EXCEPT(assister_nba_id,scorer_nba_id),COALESCE(e.assister_nba_id,a.nba_player_id) assister_nba_id,COALESCE(e.scorer_nba_id,s.nba_player_id) scorer_nba_id,a.evidence_uri assister_identity_evidence_uri,s.evidence_uri scorer_identity_evidence_uri FROM `{self.prefix}.espn_current_assists` e LEFT JOIN `{self.prefix}.espn_player_mappings` a ON a.source_player_id=e.assister_source_id LEFT JOIN `{self.prefix}.espn_player_mappings` s ON s.source_player_id=e.scorer_source_id",
            }
        )
        for name, query in views.items():
            # Do not overwrite a view changed by another operator.
            from google.cloud import bigquery

            table = bigquery.Table(f"{self.prefix}.{name}")
            table.view_query = query
            existing = self.cloud.bq.create_table(table, exists_ok=True, timeout=30)
            require(
                existing.view_query.strip() == query.strip(), f"ESPN view drift: {name}"
            )

    def register(self, game):
        self.register_games([game])

    def remember_players(self, mappings, evidence):
        """Insert-only crosswalk; conflicts roll back the entire batch."""
        attempt = uuid4().hex
        try:
            return self._remember_players(mappings, evidence, attempt)
        except (ConfirmedQueryFailure, ValueError, KeyError, TypeError) as exc:
            self.cloud.query(
                f"INSERT INTO `{self.prefix}.espn_identity_attempts` VALUES(@attempt,'rejected',0,@error,@evidence,CURRENT_TIMESTAMP())",
                [
                    self.cloud.parameter(k, v)
                    for k, v in {
                        "attempt": attempt,
                        "error": str(exc),
                        "evidence": evidence,
                    }.items()
                ],
            )
            return {"status": "rejected", "error": str(exc), "inserted": 0}

    def _remember_players(self, mappings, evidence, attempt):
        require(0 < len(mappings) <= 10000, "Invalid player mapping batch")
        from scripts.espn_sources import identifier

        for row in mappings:
            identifier(row["source_player_id"])
            identifier(row["nba_player_id"])
            require(
                row["normalized_name"] and row["method"] and row["catalog_sha256"],
                "Incomplete player mapping provenance",
            )
        result = self.cloud.query(
            f"""BEGIN TRANSACTION;
UPDATE `{self.prefix}.gamebook_store_control` SET generation=generation+1 WHERE store_id='main';
CREATE TEMP TABLE candidate_players AS SELECT JSON_VALUE(p,'$.source_player_id') source_player_id,JSON_VALUE(p,'$.nba_player_id') nba_player_id,JSON_VALUE(p,'$.normalized_name') normalized_name,JSON_VALUE(p,'$.method') method,JSON_VALUE(p,'$.catalog_sha256') catalog_sha256 FROM UNNEST(JSON_QUERY_ARRAY(@players)) p;
ASSERT NOT EXISTS(SELECT source_player_id FROM candidate_players GROUP BY 1 HAVING COUNT(*)!=1) AS 'duplicate_espn_player';
ASSERT NOT EXISTS(SELECT nba_player_id FROM candidate_players GROUP BY 1 HAVING COUNT(*)!=1) AS 'duplicate_nba_player';
ASSERT NOT EXISTS(SELECT 1 FROM candidate_players p JOIN `{self.prefix}.espn_player_mappings` m ON p.source_player_id=m.source_player_id OR p.nba_player_id=m.nba_player_id WHERE p.source_player_id!=m.source_player_id OR p.nba_player_id!=m.nba_player_id OR p.normalized_name!=m.normalized_name) AS 'player_mapping_conflict';
ASSERT NOT EXISTS(SELECT 1 FROM candidate_players p JOIN `{self.prefix}.espn_current_player_box_scores` b USING(source_player_id) WHERE b.nba_player_id IS NOT NULL AND b.nba_player_id!=p.nba_player_id) AS 'accepted_player_mapping_conflict';
CREATE TEMP TABLE additions AS SELECT p.* FROM candidate_players p WHERE NOT EXISTS(SELECT 1 FROM `{self.prefix}.espn_player_mappings` m WHERE m.source_player_id=p.source_player_id);
INSERT INTO `{self.prefix}.espn_player_mappings` SELECT *,@evidence FROM additions;
INSERT INTO `{self.prefix}.espn_identity_attempts` SELECT @attempt,IF(COUNT(*)=0,'noop','published'),COUNT(*),NULL,@evidence,CURRENT_TIMESTAMP() FROM additions;
COMMIT TRANSACTION;
SELECT COUNT(*) inserted FROM additions;""",
            [
                self.cloud.parameter("players", canonical(mappings)),
                self.cloud.parameter("evidence", evidence),
                self.cloud.parameter("attempt", attempt),
            ],
        )[0]
        return {"status": "published" if result["inserted"] else "noop", **result}

    def register_games(self, games):
        if not games:
            return
        self.cloud.query(
            f"""BEGIN TRANSACTION;
UPDATE `{self.prefix}.gamebook_store_control` SET generation=generation+1 WHERE store_id='main';
CREATE TEMP TABLE discovered AS SELECT JSON_VALUE(g,'$.source_game_id') source_game_id,JSON_VALUE(g,'$.season') season,CAST(JSON_VALUE(g,'$.date') AS DATE) game_date,JSON_VALUE(g,'$.home') home,JSON_VALUE(g,'$.away') away FROM UNNEST(JSON_QUERY_ARRAY(@games)) g;
ASSERT NOT EXISTS(SELECT source_game_id FROM discovered GROUP BY source_game_id HAVING COUNT(*)!=1) AS 'duplicate_discovered_id';
ASSERT NOT EXISTS(SELECT 1 FROM discovered d JOIN `{self.prefix}.espn_coverage` c USING(source_game_id) WHERE d.season!=c.season OR d.game_date!=c.game_date OR d.home!=c.home OR d.away!=c.away) AS 'espn_scope_changed';
INSERT INTO `{self.prefix}.espn_coverage` SELECT source_game_id,season,game_date,home,away,NULL,'pending',NULL FROM discovered d WHERE NOT EXISTS(SELECT 1 FROM `{self.prefix}.espn_coverage` c WHERE c.source_game_id=d.source_game_id);
COMMIT TRANSACTION;""",
            [self.cloud.parameter("games", canonical(games))],
        )

    def mappings(self, season):
        return self.cloud.query(
            f"SELECT source_game_id,nba_game_id,record,evidence_uri FROM `{self.prefix}.espn_mappings` WHERE SUBSTR(nba_game_id,4,2)=@year LIMIT 1501",
            [self.cloud.parameter("year", season[2:4])],
        )

    def remember_mapping(self, game, nba_id, record, evidence):
        self.cloud.query(
            f"""BEGIN TRANSACTION;
UPDATE `{self.prefix}.gamebook_store_control` SET generation=generation+1 WHERE store_id='main';
ASSERT NOT EXISTS(SELECT 1 FROM `{self.prefix}.espn_mappings` WHERE (source_game_id=@id AND nba_game_id!=@nba) OR (nba_game_id=@nba AND source_game_id!=@id)) AS 'mapping_conflict';
INSERT INTO `{self.prefix}.espn_mappings` SELECT @id,@nba,@record,@evidence FROM UNNEST([1]) WHERE NOT EXISTS(SELECT 1 FROM `{self.prefix}.espn_mappings` WHERE source_game_id=@id);
COMMIT TRANSACTION;""",
            [
                self.cloud.parameter(k, v)
                for k, v in {
                    "id": game["source_game_id"],
                    "nba": nba_id,
                    "record": canonical(record),
                    "evidence": evidence,
                }.items()
            ],
        )

    def failure(self, game, error, evidence, revision=None):
        attempt = uuid4().hex
        self.cloud.query(
            f"""BEGIN TRANSACTION;
UPDATE `{self.prefix}.gamebook_store_control` SET generation=generation+1 WHERE store_id='main';
INSERT INTO `{self.prefix}.espn_attempts` VALUES(@attempt,@id,@revision,'rejected',@error,@evidence,CURRENT_TIMESTAMP());
UPDATE `{self.prefix}.espn_coverage` SET health='failed',last_attempt_id=@attempt WHERE source_game_id=@id;
COMMIT TRANSACTION;""",
            [
                self.cloud.parameter(k, v)
                for k, v in {
                    "attempt": attempt,
                    "id": game["source_game_id"],
                    "revision": revision,
                    "error": error,
                    "evidence": evidence,
                }.items()
            ],
        )
        return {
            "source_game_id": game["source_game_id"],
            "status": "rejected",
            "error": error,
            "attempt_id": attempt,
        }

    def publish(self, candidate, evidence, inject_failure=False):
        require(
            not inject_failure
            or self.cloud.dataset.startswith("nba_gamebook_dev_espn_test_"),
            "Fault injection only in synthetic test datasets",
        )
        game = candidate["game"]
        try:
            revision = self.validate_candidate(candidate)
        except (ValueError, KeyError, TypeError) as exc:
            return self.failure(game, str(exc), evidence)
        state = self.cloud.query(
            f"SELECT current_revision FROM `{self.prefix}.espn_coverage` WHERE source_game_id=@id",
            [self.cloud.parameter("id", game["source_game_id"])],
        )
        require(len(state) == 1, "Game not registered")
        previous = state[0]["current_revision"]
        attempt = uuid4().hex
        body = canonical(candidate)
        normalized_uri = self.cloud.upload(body.encode(), "espn_normalized", "json")
        params = [
            self.cloud.parameter(k, v)
            for k, v in {
                "id": game["source_game_id"],
                "revision": revision,
                "expected": previous,
                "payload": body,
                "evidence": evidence,
                "attempt": attempt,
                "health": self.publication_health(candidate),
            }.items()
        ]
        params.append(self.cloud.parameter("inject_failure", inject_failure, "BOOL"))
        params.append(self.cloud.parameter("source_only", self.source_only, "BOOL"))
        try:
            rows = self.cloud.query(
                f"""BEGIN TRANSACTION;
UPDATE `{self.prefix}.gamebook_store_control` SET generation=generation+1 WHERE store_id='main';
ASSERT (SELECT current_revision IS NOT DISTINCT FROM @expected FROM `{self.prefix}.espn_coverage` WHERE source_game_id=@id) AS 'stale_espn_publisher';
ASSERT NOT EXISTS(SELECT 1 FROM UNNEST(JSON_QUERY_ARRAY(@payload,'$.players')) p JOIN `{self.prefix}.espn_player_mappings` m ON JSON_VALUE(p,'$.source_player_id')=m.source_player_id WHERE JSON_VALUE(p,'$.nba_player_id') IS DISTINCT FROM m.nba_player_id AND (NOT @source_only OR JSON_VALUE(p,'$.nba_player_id') IS NOT NULL)) AS 'published_player_crosswalk_conflict';
ASSERT NOT EXISTS(SELECT 1 FROM UNNEST(JSON_QUERY_ARRAY(@payload,'$.players')) p JOIN `{self.prefix}.espn_current_player_box_scores` b ON JSON_VALUE(p,'$.source_player_id')=b.source_player_id WHERE b.nba_player_id IS NOT NULL AND JSON_VALUE(p,'$.nba_player_id') IS DISTINCT FROM b.nba_player_id) AS 'published_player_identity_changed';
IF @revision IS DISTINCT FROM @expected THEN
 ASSERT NOT EXISTS(SELECT 1 FROM `{self.prefix}.espn_revisions` WHERE source_game_id=@id AND revision=@revision) AS 'historical_espn_replay';
 ASSERT NOT EXISTS(SELECT 1 FROM `{self.prefix}.espn_revisions` WHERE source_game_id=@id AND revision=@expected AND JSON_VALUE(payload,'$.nba_game_id') IS NOT NULL AND JSON_VALUE(payload,'$.nba_game_id') IS DISTINCT FROM JSON_VALUE(@payload,'$.nba_game_id')) AS 'nba_mapping_changed';
 ASSERT NOT EXISTS(SELECT 1 FROM `{self.prefix}.espn_revisions` WHERE source_game_id=@id AND revision=@expected AND JSON_VALUE(payload,'$.pdf_validation.status')='matched' AND JSON_VALUE(@payload,'$.pdf_validation.status')!='matched') AS 'validation_downgrade';
 INSERT INTO `{self.prefix}.espn_revisions` VALUES(@id,@revision,@payload,@evidence,CURRENT_TIMESTAMP());
 UPDATE `{self.prefix}.espn_coverage` SET current_revision=@revision,health=@health,last_attempt_id=@attempt WHERE source_game_id=@id;
 INSERT INTO `{self.prefix}.espn_attempts` VALUES(@attempt,@id,@revision,'published',NULL,@evidence,CURRENT_TIMESTAMP());
ELSE
 ASSERT (SELECT payload=@payload FROM `{self.prefix}.espn_revisions` WHERE source_game_id=@id AND revision=@revision) AS 'payload_hash_mismatch';
 INSERT INTO `{self.prefix}.espn_attempts` VALUES(@attempt,@id,@revision,'noop',NULL,@evidence,CURRENT_TIMESTAMP());
 UPDATE `{self.prefix}.espn_coverage` SET last_attempt_id=@attempt WHERE source_game_id=@id;
END IF;
ASSERT NOT @inject_failure AS 'injected_espn_failure';
COMMIT TRANSACTION;
SELECT a.status,c.health FROM `{self.prefix}.espn_attempts` a JOIN `{self.prefix}.espn_coverage` c USING(source_game_id) WHERE a.attempt_id=@attempt;""",
                params,
            )
            return {
                "source_game_id": game["source_game_id"],
                "status": rows[0]["status"],
                "health": rows[0]["health"],
                "revision": revision,
                "attempt_id": attempt,
                "normalized_uri": normalized_uri,
                "pdf_validation": candidate["pdf_validation"]["status"],
            }
        except ConfirmedQueryFailure as exc:
            return self.failure(game, str(exc), evidence, revision)
