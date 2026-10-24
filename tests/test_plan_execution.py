import json
from pathlib import Path
from types import SimpleNamespace

from app.agent.semantic_planner import plan_schema
from scripts.evaluate_semantic_language import mismatches
from tests.semantic_intent_fixtures import migrate_legacy_plan

SOURCE = Path(__file__).parent / "fixtures/semantics/cases.json"


def client_for(plan):
    def create(**kwargs):
        assert kwargs["store"] is False
        assert kwargs["text"]["format"]["strict"] is True
        return SimpleNamespace(
            output_text=json.dumps(migrate_legacy_plan(plan, kwargs))
        )

    return SimpleNamespace(responses=SimpleNamespace(create=create))


def query_plan():
    return {
        "status": "query",
        "message": "",
        "queries": [
            {
                "metric": "pts",
                "season": "2024-25",
                "aggregation": "average",
                "player_name": "resolved_player_1",
            }
        ],
    }


def test_scope_schema_restricts_window_and_limit():
    fields = plan_schema()["properties"]["queries"]["items"]["properties"]
    assert "season_to_date" in fields["window"]["enum"]
    assert "full season" not in fields["window"]["enum"]
    assert fields["limit"]["minimum"] == 1


def test_grader_does_not_pass_wrong_scope_or_zero_for_missing():
    assert mismatches(
        {"scope": {"season": "2025-26"}, "value": 0},
        {"scope.season": "2024-25", "value": None},
    ) == ["scope.season", "value"]
