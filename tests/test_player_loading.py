"""Ordering and failure contracts for concurrent profile reads."""

from threading import Barrier

import pytest
from app.repository._bigquery import BigQueryWarehouseRepository


def test_independent_player_reads_overlap(monkeypatch):
    repo = object.__new__(BigQueryWarehouseRepository)
    barrier = Barrier(4, timeout=3)
    identity = {"player_id": 7}
    monkeypatch.setattr(repo, "_fetch_player_identity", lambda _: identity)

    def independent(value):
        def read(*args, **kwargs):
            barrier.wait()  # A sequential implementation cannot pass this barrier.
            return value

        return read

    monkeypatch.setattr(repo, "_fetch_player_detail_row", independent({}))
    monkeypatch.setattr(
        repo, "_fetch_player_game_log_payload", independent({"games": []})
    )
    monkeypatch.setattr(repo, "_fetch_player_trends", independent([]))
    monkeypatch.setattr(repo, "_fetch_similarity_anchor", independent({"player_id": 7}))
    monkeypatch.setattr(repo, "_fetch_chart_baseline_row", lambda: {})

    def similar(player_id, *, anchor):
        assert player_id == anchor["player_id"] == 7
        return "fresh", None, []

    monkeypatch.setattr(repo, "_get_similar_players", similar)
    monkeypatch.setattr(repo, "_build_player_detail_payload", lambda **kwargs: kwargs)
    result = repo.get_player_detail(7)
    assert result["identity"] is identity
    assert result["game_log"] == {"games": []}
    assert result["similarity_state"] == "fresh"


def test_unknown_player_does_not_start_dependent_reads(monkeypatch):
    repo = object.__new__(BigQueryWarehouseRepository)
    monkeypatch.setattr(repo, "_fetch_player_identity", lambda _: None)
    monkeypatch.setattr(
        repo, "_fetch_player_detail_row", lambda _: pytest.fail("unexpected query")
    )
    assert repo.get_player_detail(999) is None
