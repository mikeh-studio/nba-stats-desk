"""Chart selection cannot change values, mix units, or manufacture missing data."""

import json
from types import SimpleNamespace

from app.agent.visualization import VisualizationAgent, candidates, study_charts
from tests.test_research_insights import presentation_study


def test_study_candidates_preserve_scope_counts_and_percentage_units():
    study = presentation_study()
    charts = study_charts(study, {"pts", "ast", "reb"}, [{"metric": "ast"}])
    assert [c["id"] for c in charts] == ["ast", "pts", "reb"]
    assert charts[1]["series"][0]["points"][1] == dict(
        x="Luka Doncic out", y=15.5, meta="2 games"
    )
    assert "2025-12-31" in charts[0]["description"]
    metric = study["metrics"][0]
    metric.update(unit="percentage points")
    assert study_charts(study, {"pts"}, [])[0]["y_label"].endswith("(%)")
    metric["descriptive"]["difference"] = None
    assert study_charts(study, {"pts"}, []) == []


class Selector:
    def __init__(self, choice):
        self.choice = choice
        self.calls = []

    def _get_client(self, provider):
        return self

    def _request_timeout_seconds(self, provider):
        return 30

    def _create_response(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            output_text=json.dumps({"chart_id": self.choice}), usage=None
        )


def test_specialist_selects_only_verified_candidate_and_falls_back_safely():
    for choice, expected, selection in [
        ("ast", "ast", "model"),
        ("invented", "pts", "fallback"),
    ]:
        options = study_charts(presentation_study(), {"pts", "ast"}, [])
        agent = Selector(choice)
        payload = VisualizationAgent().enrich(
            agent,
            {"_chart_candidates": options},
            "assists without teammate",
            "openai",
            "test",
        )
        assert payload["charts"][0]["series"][0]["key"] == expected
        assert payload["visualization"]["selection"] == selection
        assert "_chart_candidates" not in payload
        assert agent.calls[0]["tools"] is None
        assert agent.calls[0]["timeout_seconds"] == 10
        assert (
            payload["charts"][0]["series"]
            == next(c for c in options if c["id"] == expected)["series"]
        )


def test_no_evidence_means_no_chart_or_model_call():
    agent = Selector("none")
    payload = {"answer": "Unavailable", "charts": []}
    assert (
        VisualizationAgent().enrich(agent, payload, "draw anything", "openai", "test")
        == payload
    )
    assert not agent.calls


def test_chronological_evidence_keeps_line_chart_and_metadata():
    payload = dict(
        semantic_evidence={"scope": {"operation": "game_log"}},
        charts=[
            dict(
                type="line",
                title="Points by game",
                series=[dict(points=[dict(x="2025-11-01", y=-2, meta="game-1")])],
            )
        ],
    )
    result = candidates(payload)
    assert result[0]["type"] == "line"
    assert result[0]["series"][0]["points"][0]["y"] == -2
    assert "chronological" in result[0]["description"]


def test_ranking_uses_typed_values_and_refuses_missing_components():
    payload = dict(
        semantic_evidence=dict(
            scope=dict(operation="rank", aggregation="average"),
            metric=dict(key="pts", label="Points", unit="count"),
            rows=[
                dict(display_value=30, valid_games=50, missing_component_games=0),
                dict(display_value=20, valid_games=45, missing_component_games=0),
            ],
        ),
        tables=[
            dict(
                title="2025-26 Regular Season",
                rows=[["Player A", "30.0"], ["Player B", "20.0"]],
            )
        ],
    )
    chart = candidates(payload)[0]
    assert chart["type"] == "bar"
    assert chart["series"][0]["points"][0]["y"] == 30
    assert "2025-26" in chart["description"]
    payload["semantic_evidence"]["rows"][0]["missing_component_games"] = 1
    assert candidates(payload) == []
