"""Independent date and value regressions for the Opus review findings."""

import json
from types import SimpleNamespace

import pytest
from app.agent.semantics import Evidence, SemanticError
from fastapi.testclient import TestClient
from tests import test_performance_overview as overview_tests
from tests.test_semantic_migration import Model, settings


@pytest.fixture
def evidence():
    return overview_tests.evidence.__wrapped__()


class BoundedWarehouse:
    def __init__(self, original, missing=None):
        self.rows = [
            dict(
                r,
                game_date=f"2025-01-{r['game_date'][-2:]}"
                if r["season"] == "2024-25"
                else r["game_date"],
            )
            for r in original.rows
        ]
        self.coverage = original.covered_scopes
        self.through = original.data_through
        self.calls = []
        self.missing = missing

    def load(self, seasons):
        self.calls.append(seasons)
        if self.missing in seasons:
            raise SemanticError("unsupported_coverage", "Required archive unavailable")
        scopes = frozenset(s for s in self.coverage if s[0] in seasons)
        return {}, Evidence(
            [r for r in self.rows if r["season"] in seasons],
            scopes,
            "fixture",
            "bounded-review",
            {s: self.through[s] for s in scopes},
            complete=True,
        )


@pytest.mark.parametrize("suffix", ["", "/stream"])
@pytest.mark.parametrize(
    "question,expected,seasons",
    [
        ("How many points did Player 1 average in 2025?", 15, ["2025-26", "2024-25"]),
        ("Player 1 points past year as of 2025-12-31", 15, ["2025-26", "2024-25"]),
        ("Player 1 points in January as of 2025-12-31", 10, ["2024-25"]),
    ],
)
def test_metric_inferred_windows_use_all_required_archives_at_endpoints(
    evidence, suffix, question, expected, seasons
):
    from app.main import app, get_agent_client, get_repository, get_settings
    from app.repository import BigQueryWarehouseRepository

    source = BoundedWarehouse(evidence)
    config = settings("2025-26")
    repo = BigQueryWarehouseRepository(config, client=SimpleNamespace())
    repo._governed_warehouse = source
    old = dict(app.dependency_overrides)
    app.dependency_overrides.update(
        {
            get_settings: lambda: config,
            get_repository: lambda: repo,
            get_agent_client: lambda: Model(),
        }
    )
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/agent/ask" + suffix, json={"question": question}
            )
            assert response.status_code == 200
            payload = (
                response.json()
                if not suffix
                else next(
                    json.loads(line[6:])["payload"]
                    for line in response.text.splitlines()
                    if line.startswith("data: ")
                    and json.loads(line[6:]).get("type") == "final"
                )
            )
            assert payload["status"] == "ok", payload
            assert payload["semantic_evidence"]["rows"][0]["value"] == expected
            assert payload["semantic_evidence"]["scope"]["seasons"] == seasons
            assert source.calls[0] == seasons
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(old)


@pytest.mark.parametrize("missing", ["2024-25", "2025-26"])
def test_metric_calendar_year_withholds_if_any_required_archive_is_missing(
    evidence, missing
):
    from app.agent.semantic_answer import SemanticAsk

    model = Model()
    payload = SemanticAsk(
        settings("2025-26"), BoundedWarehouse(evidence, missing)
    ).answer("Player 1 points in 2025", client=model, model="fixture")
    assert payload["status"] == "unsupported_coverage"
    assert not payload.get("semantic_evidence")
    assert not model.calls


def test_metric_dates_conflicting_with_named_season_do_not_widen(evidence):
    from app.agent.semantic_answer import SemanticAsk

    source = BoundedWarehouse(evidence)
    payload = SemanticAsk(settings("2025-26"), source).answer(
        "Player 1 points from 2025-01-01 through 2025-12-31 in the 2025-26 season",
        client=Model(),
        model="fixture",
    )
    assert payload["status"] == "clarification_required"
    assert source.calls == []


class CalendarComparisonModel(Model):
    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            output_text=json.dumps(
                {
                    "plan": {
                        "status": "compare",
                        "message": "",
                        "queries": [
                            {
                                "metric": "pts",
                                "season": "2025-26",
                                "aggregation": "average",
                                "player_name": "resolved_player_1",
                                "window": "date_range",
                                "start_date": start,
                                "as_of": end,
                            }
                            for start, end in [
                                ("2025-01-01", "2025-01-31"),
                                ("2025-11-01", "2025-11-30"),
                            ]
                        ],
                    }
                }
            )
        )


def test_calendar_metric_comparison_reaches_planner_and_loads_both_archives(evidence):
    from app.agent.semantic_answer import SemanticAsk

    source = BoundedWarehouse(evidence)
    model = CalendarComparisonModel()
    payload = SemanticAsk(settings("2025-26"), source).answer(
        "Compare Player 1 points in January 2025 vs November 2025",
        client=model,
        model="fixture",
    )
    assert payload["status"] == "ok", payload
    assert len(model.calls) == 1
    result = payload["semantic_evidence"]
    assert result["current"]["rows"][0]["value"] == 10
    assert result["baseline"]["rows"][0]["value"] == 20
    assert source.calls[-1] == ["2025-26", "2024-25"]


def test_since_month_request_keeps_model_dates_and_full_archive_scope(evidence):
    from app.agent.semantic_answer import SemanticAsk

    source = BoundedWarehouse(evidence)
    model = Model(window="date_range", start_date="2025-03-01", as_of="2025-11-30")
    payload = SemanticAsk(settings("2025-26"), source).answer(
        "Player 1 points since March 2025", client=model, model="fixture"
    )
    assert payload["status"] == "ok", payload
    assert len(model.calls) == 1
    assert payload["semantic_evidence"]["scope"]["window_start"] == "2025-03-01"
    assert payload["semantic_evidence"]["rows"][0]["value"] == 20
    assert source.calls[-1] == ["2025-26", "2024-25"]


def test_model_date_range_without_end_retains_matching_phase_source_anchor(evidence):
    from app.agent.semantic_answer import SemanticAsk

    payload = SemanticAsk(settings("2025-26"), BoundedWarehouse(evidence)).answer(
        "Player 1 points since March 2025",
        client=Model(window="date_range", start_date="2025-03-01", as_of=None),
        model="fixture",
    )
    assert payload["status"] == "ok"
    assert payload["semantic_evidence"]["scope"]["as_of"] == "2025-11-05"
    assert payload["semantic_evidence"]["rows"][0]["value"] == 20
