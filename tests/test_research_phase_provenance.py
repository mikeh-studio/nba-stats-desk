"""Synthetic HTTP controls: model provenance labels cannot authorize phase."""

import pytest
from app.agent.research_ask import resolve_phase
from tests.test_descriptive_research import plan, request
from tests.test_descriptive_research import setup as _setup


@pytest.fixture
def phase_endpoint(monkeypatch):
    yield from _setup.__wrapped__(monkeypatch)


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize("label", ["explicit", "inherited", "omitted"])
@pytest.mark.parametrize("proposed", [None, "Regular Season"])
def test_omitted_phase_uses_server_default(phase_endpoint, transport, label, proposed):
    client, planner, _, store = phase_endpoint
    planner.result = {
        **plan(phase=proposed),
        "kind": "descriptive",
        "phase_source": label,
    }
    payload = request(
        client,
        "At home, research Morgan Fiction's points for 2025-26",
        transport,
        conversation_id="phase-origin",
    )
    assert payload["research_scope"]["phase"] == "Regular Season"
    result = payload["research"]["players"][0]["metrics"][0]["result"]
    assert result["value"] == 4
    assert result["valid_games"] == 2
    provenance = store.get_turns("phase-origin", max_turns=1)[0].context[
        "research_scope_provenance"
    ]
    assert provenance["phase_source"] == "default"
    assert provenance["phase_user_selected"] is False
    planner.result["query"]["phase"] = None
    refused = request(
        client,
        "Research that same set of games again",
        transport,
        conversation_id="phase-origin",
    )
    assert refused["research_error_code"] == "invalid_research_plan"
    assert not refused["tables"]
    planner.result["query"]["phase"] = "Regular Season"
    followup = request(
        client,
        "Research that same set of games again",
        transport,
        conversation_id="phase-origin",
    )
    assert followup["research_scope"]["phase"] == "Regular Season"
    result = followup["research"]["players"][0]["metrics"][0]["result"]
    assert result["value"] == 4
    assert result["valid_games"] == 2
    provenance = store.get_turns("phase-origin", max_turns=1)[0].context[
        "research_scope_provenance"
    ]
    assert provenance["phase_source"] == "inherited"
    assert provenance["phase_user_selected"] is False


@pytest.mark.parametrize(
    "question,previous,user_selected",
    [
        ("Research points during the playoffs", {}, False),
        ("Research those games again", {"phase": "Playoffs"}, True),
        ("Research those games again", {"phase": "Regular Season"}, False),
    ],
)
def test_explicit_or_inherited_phase_requires_concrete_value(
    question, previous, user_selected
):
    with pytest.raises(ValueError, match="Planned phase contradicts"):
        resolve_phase(question, None, "descriptive", previous, user_selected)


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize(
    "phase,wording", [("Playoffs", "playoffs"), ("Both", "both phases")]
)
@pytest.mark.parametrize("label", ["explicit", "inherited", "omitted"])
def test_explicit_and_inherited_values_ignore_advisory_labels(
    phase_endpoint, transport, phase, wording, label
):
    client, planner, _, store = phase_endpoint
    planner.result = {**plan(phase=phase), "kind": "descriptive", "phase_source": label}
    first = request(
        client,
        f"Research Morgan Fiction's home points, 2025-26 {wording}",
        transport,
        conversation_id="phase-origin",
    )
    assert first["research_scope"]["phase"] == phase
    provenance = store.get_turns("phase-origin", max_turns=1)[0].context[
        "research_scope_provenance"
    ]
    assert provenance["phase_source"] == "explicit"
    assert provenance["phase_user_selected"] is True
    followup = request(
        client,
        "Research that same set of games again",
        transport,
        conversation_id="phase-origin",
    )
    assert followup["research_scope"]["phase"] == phase
    for payload in (first, followup):
        result = payload["research"]["players"][0]["metrics"][0]["result"]
        assert result["value"] == pytest.approx(90 if phase == "Playoffs" else 98 / 3)
        assert result["valid_games"] == (1 if phase == "Playoffs" else 3)
    provenance = store.get_turns("phase-origin", max_turns=1)[0].context[
        "research_scope_provenance"
    ]
    assert provenance["phase_source"] == "inherited"
    assert provenance["phase_user_selected"] is True
    planner.result["query"]["phase"] = "Regular Season"
    bad = request(
        client,
        "Research that same set of games again",
        transport,
        conversation_id="phase-origin",
    )
    assert bad["research_error_code"] == "invalid_research_plan"
    assert not bad["tables"]


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize("label", ["explicit", "inherited", "omitted"])
@pytest.mark.parametrize(
    "suffix,proposed",
    [
        ("", "Playoffs"),
        ("", "Both"),
        (" during the playoffs", "Both"),
        (" in both phases", "Regular Season"),
    ],
)
def test_labels_cannot_authorize_contradictory_values(
    phase_endpoint, transport, label, suffix, proposed
):
    client, planner, _, _ = phase_endpoint
    planner.result = {
        **plan(phase=proposed),
        "kind": "descriptive",
        "phase_source": label,
    }
    payload = request(
        client,
        "Research Morgan Fiction's points at home for 2025-26" + suffix,
        transport,
    )
    assert payload["research_error_code"] == "invalid_research_plan"
    assert not payload["tables"]


@pytest.mark.parametrize("transport", ["json", "sse"])
@pytest.mark.parametrize(
    "exclusion", ["except", "other than", "not counting", "minus", "apart from"]
)
def test_phase_exclusion_refuses_ambiguous_combination(
    phase_endpoint, transport, exclusion
):
    client, planner, _, _ = phase_endpoint
    planner.result = {**plan(phase="Both"), "kind": "descriptive"}
    positive = request(
        client,
        "Research Morgan Fiction points at home in 2025-26 regular season and playoffs",
        transport,
    )
    result = positive["research"]["players"][0]["metrics"][0]["result"]
    assert result["value"] == pytest.approx(98 / 3)
    assert result["valid_games"] == 3
    for phase in ("Both", "Regular Season"):
        planner.result["query"]["phase"] = phase
        negative = request(
            client,
            f"Research Morgan Fiction points at home in 2025-26 regular season {exclusion} playoffs",
            transport,
        )
        assert negative["research_error_code"] == "invalid_research_plan"
        assert not negative["tables"]
