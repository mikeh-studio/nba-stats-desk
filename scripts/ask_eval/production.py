"""Replay captured production evidence without warehouse access during evaluation."""

import copy
import json
from types import SimpleNamespace


def production_repository(settings, source):
    from app.agent.semantic_source import snapshot_evidence
    from app.agent.semantics import SemanticError
    from app.repository import BigQueryWarehouseRepository

    snapshot = source["snapshot"]
    evidence = snapshot_evidence(snapshot)

    class Warehouse:
        def load(self, seasons):
            if not set(seasons).issubset({c["season"] for c in snapshot["coverage"]}):
                raise SemanticError(
                    "unsupported_coverage", "Season not captured for this evaluation"
                )
            return copy.deepcopy(snapshot), snapshot_evidence(copy.deepcopy(snapshot))

    class Repository(BigQueryWarehouseRepository):
        def __init__(self):
            super().__init__(settings, client=SimpleNamespace())
            self._governed_warehouse = Warehouse()

        def _query(self, *args, **kwargs):
            raise RuntimeError("Uncaptured repository read: no live warehouse fallback")

        def search_players(self, query, limit=12):
            from app.agent.player_resolver import normalize_player_text

            players = {
                r["player_id"]: {
                    k: r.get(k) for k in ("player_id", "player_name", "team_abbr")
                }
                for r in evidence.rows
                if r["season"] == settings.season
            }
            needle = normalize_player_text(query)
            return [
                p
                for p in players.values()
                if needle in normalize_player_text(p["player_name"])
            ][:limit]

        def get_player_detail(self, player_id):
            return copy.deepcopy(source["details"].get(str(player_id)))

        def get_player_game_log(
            self, player_id, limit=30, *, start_date=None, end_date=None
        ):
            rows = [
                r
                for r in evidence.rows
                if r["season"] == settings.season
                and r["player_id"] == player_id
                and (not start_date or r["game_date"] >= start_date)
                and (not end_date or r["game_date"] <= end_date)
            ]
            rows.sort(key=lambda r: (r["game_date"], r["game_id"]), reverse=True)
            return {"games": copy.deepcopy(rows[:limit])}

    return Repository()


def validate_production_source(source):
    from app.agent.semantic_source import snapshot_evidence

    snapshot_evidence(source["snapshot"])
    if source["season"] not in {c["season"] for c in source["snapshot"]["coverage"]}:
        raise ValueError("Selected season is not captured")
    # Serialize now to fail before any provider call if the capture is malformed.
    json.dumps(source, allow_nan=False)
