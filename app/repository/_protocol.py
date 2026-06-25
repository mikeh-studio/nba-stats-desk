from __future__ import annotations

from typing import Any, Protocol

from app.repository._constants import (
    SIMILARITY_RESULT_LIMIT,
    CompareFocus,
    CompareWindow,
)


class WarehouseRepository(Protocol):
    def get_dashboard(self, as_of_date: str | None = None) -> dict[str, Any]: ...

    def get_leaderboard(self, limit: int = 10) -> list[dict[str, Any]]: ...

    def get_trends(self, limit: int = 10) -> list[dict[str, Any]]: ...

    def get_recommendations(
        self, limit: int = 10, insight_type: str | None = None
    ) -> list[dict[str, Any]]: ...

    def get_rankings(self, limit: int = 25) -> list[dict[str, Any]]: ...

    def search_players(self, query: str, limit: int = 10) -> list[dict[str, Any]]: ...

    def list_agent_player_candidates(
        self, limit: int = 1000
    ) -> list[dict[str, Any]]: ...

    def get_player_detail(self, player_id: int) -> dict[str, Any] | None: ...

    def get_compare(
        self,
        player_a_id: int,
        player_b_id: int,
        *,
        window: CompareWindow = "last_5",
        focus: CompareFocus = "balanced",
    ) -> dict[str, Any]: ...

    def get_latest_analysis(self) -> dict[str, Any] | None: ...

    def get_latest_successful_run(self) -> dict[str, Any] | None: ...

    def get_season_coverage(self) -> dict[str, Any] | None: ...

    def get_player_game_log(
        self,
        player_id: int,
        limit: int = 30,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any] | None: ...

    def get_recent_performance_initial(
        self,
        *,
        game_date: str | None = None,
        game_id: str | None = None,
        limit: int = 240,
    ) -> dict[str, Any]: ...

    def get_recent_performance_dates(self) -> list[dict[str, Any]]: ...

    def get_recent_performance_games(
        self, *, game_date: str | None = None
    ) -> list[dict[str, Any]]: ...

    def get_recent_performance_players(
        self,
        *,
        game_date: str,
        game_id: str | None = None,
        limit: int = 240,
    ) -> list[dict[str, Any]]: ...

    def get_recent_performance_player(
        self, player_id: int, *, game_id: str
    ) -> dict[str, Any] | None: ...

    def get_metric_leaders(
        self, metric: str, limit: int = 10
    ) -> list[dict[str, Any]]: ...

    def get_player_metric_percentile(
        self, player_id: int, metric: str, min_games: int = 5
    ) -> dict[str, Any] | None: ...

    def get_similarity_map(self) -> dict[str, Any]: ...

    def get_similarity_neighbors(
        self, player_id: int, *, limit: int = SIMILARITY_RESULT_LIMIT
    ) -> dict[str, Any]: ...

    def get_health(self) -> dict[str, Any]: ...
