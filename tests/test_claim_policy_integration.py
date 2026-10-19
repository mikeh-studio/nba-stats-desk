"""Actual endpoints combine the approved default with exact G assumptions."""

import pytest
from app.agent.claims import expand_path, validate
from tests.test_dated_points_default import endpoint  # noqa: F401


@pytest.mark.parametrize("transport", ["json", "sse"])
def test_default_points_assumption_preserves_exact_following_bindings(
    request, transport
):
    payload = request.getfixturevalue("endpoint")(transport=transport)
    assert payload["status"] == "ok"
    assert (
        payload["query_plan"]["aggregation_policy"]["chosen_aggregation"] == "average"
    )
    assert payload["semantic_evidence"]["rows"][0]["value"] == 3.5
    manifest = payload["claim_bindings"]
    destinations = [expand_path(manifest, c["d"]) for c in manifest["claims"]]
    assert ["assumptions", 1] in destinations
    assert [
        "assumptions",
        0,
    ] not in destinations  # Policy prose remains an explicit gap.
    assert validate(payload)["errors"] == []
    payload["assumptions"][1] = payload["assumptions"][1].replace(
        "Regular Season", "Playoffs"
    )
    assert validate(payload)["status"] == "invalid"
