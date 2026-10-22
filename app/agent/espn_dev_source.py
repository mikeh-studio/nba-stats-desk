"""Read-only, explicitly partial ESPN box-score evidence for local Ask review."""

from __future__ import annotations

import json
import math
from collections import Counter

from scripts.espn_corrections import validate as validate_box

from app.agent.semantic_source import (
    BigQuerySemanticSource,
    capture_timestamp,
    snapshot_digest,
)
from app.agent.semantics import SemanticError
from app.seasons import DEFAULT_SEASON

DEV_DATASET = "nba_gamebook_dev_espn_only"
FIELDS = dict(
    pts="PTS",
    reb="REB",
    ast="AST",
    stl="STL",
    blk="BLK",
    tov="TO",
    fg3m="FG3M",
    fgm="FGM",
    fga="FGA",
    ftm="FTM",
    fta="FTA",
    fg3a="FG3A",
    plus_minus="PLUS_MINUS",
)


def minutes(value):
    try:
        parts = str(value).split(":")
        if len(parts) == 1:
            result = float(parts[0])
        elif len(parts) == 2 and 0 <= float(parts[1]) < 60:
            result = float(parts[0]) + float(parts[1]) / 60
        else:
            raise ValueError("Invalid minute format")
        if not math.isfinite(result) or result < 0 or float(parts[0]) < 0:
            raise ValueError("Invalid minute value")
        return result
    except (TypeError, ValueError, OverflowError) as exc:
        raise SemanticError("invalid_evidence", "Invalid ESPN minutes") from exc


def snapshot(bundle, season, source, query_id=None):
    try:
        return _snapshot(bundle, season, source, query_id)
    except SemanticError:
        raise
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError) as exc:
        raise SemanticError(
            "invalid_evidence", "Malformed ESPN dev box-score evidence"
        ) from exc


def _snapshot(bundle, season, source, query_id=None):
    games, mappings = bundle["games"], bundle["mappings"]
    if len(games) > 1500 or len(mappings) > 1000:
        raise SemanticError("incomplete_evidence", "Dev source bound exceeded")
    identities = {}
    for mapping in mappings:
        pid = mapping["source_player_id"]
        if pid in identities:
            raise SemanticError("invalid_evidence", "Duplicate source identity")
        identities[pid] = int(mapping["nba_player_id"])
    if len(set(identities.values())) != len(identities):
        raise SemanticError("invalid_evidence", "Conflicting NBA identity mapping")
    rows, excluded, accepted, pbp_pending, seen = [], 0, 0, 0, set()
    revisions = []
    for item in games:
        gid = item["source_game_id"]
        if gid in seen or item["season"] != season:
            raise SemanticError("invalid_evidence", "Duplicate or wrong-season game")
        seen.add(gid)
        if item["box_status"] != "accepted" or item["result_status"] != "accepted":
            continue
        candidate = json.loads(item["payload"])
        # A matching hash establishes integrity, not the source contract. Rebuild
        # and validate the box from its retained input before serving any rows.
        digest = validate_box(candidate)
        if digest != item["box_revision"]:
            raise SemanticError("invalid_evidence", "Box revision hash differs")
        game = candidate["game"]
        if (
            candidate["layer"] != "box"
            or game["source_game_id"] != gid
            or game["season"] != season
            or candidate["result_revision"] != item["result_revision"]
        ):
            raise SemanticError("invalid_evidence", "Box dependency differs")
        accepted += 1
        pbp_pending += item["pbp_status"] != "accepted"
        revisions.append(
            {
                "source_game_id": gid,
                "box_revision": item["box_revision"],
                "result_revision": item["result_revision"],
                "pbp_status": item["pbp_status"],
                "pdf_verification_status": item["pdf_verification_status"],
            }
        )
        for player in candidate["players"]:
            if not player["played"]:
                continue
            if player["source_player_id"] not in identities:
                excluded += 1
                continue
            side = "home" if player["team"] == game["home"] else "away"
            if player["team"] != game[side]:
                raise SemanticError("invalid_evidence", "Player team differs")
            stats = player["stats"]
            rows.append(
                dict(
                    season=season,
                    season_type="Regular Season",
                    game_id="espn:" + gid,
                    source_game_id=gid,
                    player_id=identities[player["source_player_id"]],
                    player_name=player["name"],
                    game_date=game["date"],
                    team_abbr=player["team"],
                    opponent_abbr=game["away" if side == "home" else "home"],
                    home_away=side.upper(),
                    min=minutes(stats["MIN"]),
                    **{k: stats[v] for k, v in FIELDS.items()},
                )
            )
    if not rows:
        raise SemanticError(
            "unsupported_coverage",
            "No accepted, NBA-mapped current-season ESPN dev box scores",
        )
    rows.sort(key=lambda r: (r["game_date"], r["game_id"], r["player_id"]))
    through = max(r["game_date"] for r in rows)
    note = (
        f"Local dev preview: ESPN-reported box scores through {through}; {accepted} accepted games out of {len(games)} observed finals. "
        f"{excluded} player-game rows excluded because NBA identity is unresolved. Coverage is an observed sample, not certified league-wide coverage. "
        f"PBP remains unreconciled for {pbp_pending} accepted-box games; box assists do not certify assist-pair relationships. League and position rankings are disabled."
    )
    counts = Counter((r["season"], r["season_type"]) for r in rows)
    result = dict(
        version=1,
        evidence_kind="espn_dev",
        sources=[source],
        source_timestamp=capture_timestamp(),
        rows=rows,
        coverage=[
            dict(season=s, phase=p, rows=n, data_through=through)
            for (s, p), n in counts.items()
        ],
        cohort_complete=False,
        coverage_notes=[note],
        review_metadata=dict(
            provider="espn",
            observed_finals=len(games),
            accepted_boxes=accepted,
            excluded_identity_rows=excluded,
            pbp_pending=pbp_pending,
            revisions=revisions,
        ),
        capture=dict(query_id=query_id, query_count=1),
        limitations=[note],
    )
    result["sha256"] = snapshot_digest(result)
    return result


class Retained2025Source(BigQuerySemanticSource):
    """Local review only: fixed previous-season read from retained base facts."""

    def source_for_season(self, season):
        if season == "2025-26":
            return f"{self.project}.{self.dataset}.fct_player_game_stats"
        return super().source_for_season(season)


class ESPNDevSource:
    def __init__(self, base):
        self.base = base

    def historical_capture(self, seasons):
        from google.api_core.exceptions import NotFound

        try:
            return self.base.capture(seasons)
        except SemanticError as exc:
            if (
                "2025-26" not in seasons
                or exc.code != "unsupported_coverage"
                or not isinstance(exc.__cause__, NotFound)
            ):
                raise
        retained = Retained2025Source(
            self.base.client,
            project=self.base.project,
            gold_dataset=self.base.dataset,
            position_context_path=self.base.position_context_path,
            max_rows=self.base.max_rows,
            maximum_bytes_billed=self.base.byte_budget,
        ).capture(seasons)
        note = (
            "Local review: the 2025–26 archive is unavailable; this answer reads "
            "retained 2025–26 warehouse facts from the base table with an explicit "
            "season filter. No archive was created or certified."
        )
        retained["coverage_notes"] = [note]
        retained["scope_assumptions"] = [note]
        retained["limitations"].append(note)
        retained["review_metadata"] = {
            "provider": "nba_warehouse",
            "retained_base_season": "2025-26",
            "archive_status": "unavailable",
        }
        retained["sha256"] = snapshot_digest(retained)
        return retained

    def capture(self, seasons):
        if DEFAULT_SEASON not in seasons:
            return self.historical_capture(seasons)
        # Missing historical baselines stay explicit; caller can retry current only.
        if seasons != [DEFAULT_SEASON]:
            self.historical_capture([s for s in seasons if s != DEFAULT_SEASON])
            raise SemanticError(
                "unsupported_coverage",
                "Mixed historical/dev evidence requires separate scopes",
            )
        from google.cloud import bigquery

        p = f"{self.base.project}.{DEV_DATASET}"
        sql = f"""SELECT ARRAY(SELECT AS STRUCT c.*,b.payload FROM `{p}.espn_layer_readiness` c LEFT JOIN `{p}.espn_layer_current` b ON c.source_game_id=b.source_game_id AND b.layer='box' WHERE c.season=@season ORDER BY c.source_game_id LIMIT 1501) games,
ARRAY(SELECT AS STRUCT source_player_id,nba_player_id FROM `{p}.espn_player_mappings` ORDER BY source_player_id LIMIT 1001) mappings"""
        job = self.base.client.query(
            sql,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("season", "STRING", DEFAULT_SEASON)
                ],
                maximum_bytes_billed=100_000_000,
                labels={"purpose": "ask-dev-review"},
            ),
        )
        bundle = dict(next(iter(job.result(timeout=60))))
        return snapshot(
            bundle, DEFAULT_SEASON, p + ".espn_layer_player_box_scores", job.job_id
        )
