"""Validate a published profile's release and game scope before rendering."""

import re
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import urlencode

from starlette.requests import Request

from app.repository._helpers import build_headshot_url
from app.seasons import DEFAULT_SEASON, validate_season
from app.serving.cutover import publication_token


class PublishedPlayerGameNotFound(LookupError):
    """The validated release does not contain exactly one matching appearance."""


@dataclass(frozen=True)
class PublishedPlayerScope:
    player_id: int
    season: str
    publication_id: str | None
    game_id: str | None
    first: date | None = None
    last: date | None = None

    @classmethod
    def from_request(cls, player_id: int, request: Request) -> "PublishedPlayerScope":
        params = request.query_params
        game_id = params.get("game_id")
        first, last = params.get("first"), params.get("last")
        if (
            player_id < 1
            or set(params) - {"season", "publication_id", "game_id", "first", "last"}
            or any(len(params.getlist(key)) != 1 for key in params)
            or (game_id is not None and not re.fullmatch(r"[0-9]{10}", game_id))
            or (first is None) != (last is None)
            or (game_id is not None and first is not None)
        ):
            raise ValueError("Invalid published player scope")
        publication_id = publication_token(request.scope)
        if (publication_id is not None and "season" not in params) or (
            (game_id is not None or first is not None)
            and ("publication_id" not in params or publication_id is None)
        ):
            raise ValueError(
                "Pinned published player scope requires an explicit season and publication"
            )
        first_day = last_day = None
        if first is not None and last is not None:
            first_day, last_day = date.fromisoformat(first), date.fromisoformat(last)
            if (
                first_day.isoformat() != first
                or last_day.isoformat() != last
                or first_day > last_day
            ):
                raise ValueError("Invalid published player dates")
        return cls(
            player_id,
            validate_season(params.get("season", DEFAULT_SEASON)),
            publication_id,
            game_id,
            first_day,
            last_day,
        )

    def url(self, suffix: str = "") -> str:
        query = urlencode(
            {
                key: value
                for key, value in {
                    "season": self.season,
                    "publication_id": self.publication_id,
                    "game_id": self.game_id,
                    "first": self.first,
                    "last": self.last,
                }.items()
                if value is not None
            }
        )
        return f"/players/{self.player_id}{suffix}?{query}"

    def view(self, release: dict[str, Any]) -> dict[str, Any]:
        scope = release["scope"]
        publication_id = validate_release(release, self.season, self.publication_id)
        identity = release["data"]["identity"]
        if identity["player_id"] != self.player_id:
            raise ValueError("Published player release unavailable")

        player_name = identity["player_name"]
        game_log = release["data"]["game_log"]
        game = None
        played = None
        source_first, source_last = (
            date.fromisoformat(scope["first"]),
            date.fromisoformat(scope["last"]),
        )
        first, last = source_first, source_last
        if self.first is not None and self.last is not None:
            first, last = self.first, self.last
            if not source_first <= first <= last <= source_last:
                raise ValueError("Published player dates outside publication")
            game_log = [
                row
                for row in game_log
                if first <= date.fromisoformat(row["game_date"]) <= last
            ]
        if self.game_id is not None:
            games = [row for row in game_log if row["game_id"] == self.game_id]
            if len(games) != 1:
                raise PublishedPlayerGameNotFound(
                    "Game unavailable in this publication"
                )
            game = games[0]
            played = date.fromisoformat(game["game_date"])
            if (
                game["player_id"] != self.player_id
                or game["player_name"] != player_name
                or game["season"] != self.season
                or game["season_type"] != scope["phase"]
                or not source_first <= played <= source_last
            ):
                raise ValueError("Published player game scope differs")

        draft = (
            f"How did {player_name} play against {game['opponent_abbr']} on {played} ({scope['phase']})?"
            if game is not None
            else f"{player_name} per game points from {first} to {last}"
        )
        draft_query = urlencode({"season": self.season, "draft": draft})
        evidence_query = urlencode(
            {"season": self.season, "publication_id": publication_id}
        )
        summary = {
            item["metric"]: item
            for item in release["data"]["summary"]["metrics"]
            if item["aggregation"] == "per_game"
        }
        return {
            "player_id": self.player_id,
            "name": player_name,
            "headshot_url": build_headshot_url(self.player_id),
            "initials": "".join(part[0] for part in player_name.split()[:2]),
            "team": game["team_abbr"] if game else ", ".join(identity["team_abbrs"]),
            "game": game,
            "game_date_label": f"{played:%b} {played.day}, {played.year}"
            if played is not None
            else None,
            "game_id": self.game_id,
            "first": self.first,
            "last": self.last,
            "game_log_count": len(game_log),
            "game_log": [
                {
                    **row,
                    "profile_url": PublishedPlayerScope(
                        self.player_id, self.season, publication_id, row["game_id"]
                    ).url(),
                }
                for row in game_log[:10]
            ],
            "observed_games": identity["observed_games"],
            "summary": summary,
            "scope": scope,
            "publication_id": publication_id,
            "release_profile_url": PublishedPlayerScope(
                self.player_id, self.season, publication_id, None
            ).url(),
            "draft_url": f"/ask?{draft_query}",
            "evidence_url": (
                f"/publication-core/api/players/{self.player_id}/game-log?{evidence_query}"
            ),
        }


def validate_release(
    release: dict[str, Any], season: str, expected_publication: str | None = None
) -> str:
    status = release["status"]
    publication_id = release["publication_id"]
    if (
        release["scope"]["season"] != season
        or not re.fullmatch(r"pub_[0-9a-f]{16}", publication_id)
        or (expected_publication is not None and publication_id != expected_publication)
        or status["publication_id"] != publication_id
        or status["scope"] != release["scope"]
        or status["freshness"] not in {"fresh", "stale_usable"}
        or status["purpose"] != "production"
    ):
        raise ValueError("Published release unavailable")
    return publication_id


def player_search_items(
    release: dict[str, Any], season: str, limit: int
) -> list[dict[str, Any]]:
    publication_id = validate_release(release, season)
    return [
        {
            "player_id": player["player_id"],
            "player_name": player["player_name"],
            "headshot_url": build_headshot_url(player["player_id"]),
            "latest_team_abbr": ", ".join(player["team_abbrs"]),
            "games_sampled": player["observed_games"],
            "profile_url": PublishedPlayerScope(
                player["player_id"], season, publication_id, None
            ).url(),
        }
        for player in release["data"][:limit]
    ]
