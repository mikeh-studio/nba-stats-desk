"""Publication must reject outputs detached from verified evidence."""

import hashlib
import json
import sys
from copy import deepcopy
from datetime import date, timedelta

import pytest
from app.agent.teammate_readiness import build_panel, summarize_panel
from scripts.export_teammate_study import export, main, validate_evidence
from scripts.teammate_association import COVARIATES, analyze
from tests.test_teammate_association import synthetic
from tests.test_teammate_readiness import fixture


def evidence():
    args = fixture()
    panel = build_panel(*args)
    spec = {**args[-1], "name": "Focal Player / Other Player"}
    injuries = {"rows": args[1]}
    plan = {
        "primary_outcome": "ast",
        "covariates": [*COVARIATES, "calendar_month_fixed_effects"],
    }
    return spec, summarize_panel(panel), analyze(panel, args[1]), panel, injuries, plan


def test_matching_evidence_validates_after_json_roundtrip():
    validate_evidence(*json.loads(json.dumps(evidence())))


def test_estimated_study_can_publish_but_changed_estimate_is_rejected():
    spec, _, _, original, injuries, plan = evidence()
    panel, reports = [], []
    for i, sample in enumerate(synthetic()):
        day = (date(2025, 11, 1) + timedelta(days=i)).isoformat()
        row = deepcopy(original[0])
        row.update(
            game_id=f"g{i}",
            game_date=day,
            scheduled_start_utc=day + "T23:00:00Z",
            exposure="reported_out_no_appearance" if sample["out"] else "participated",
            included=True,
            episode_id=sample["episode_id"],
            rest_days=sample["rest_days"],
            opponent_prior_win_pct=sample["opponent_prior_win_pct"],
            home_away="home" if sample["home"] else "away",
        )
        row["outcomes"].update(ast=sample["ast"], min=sample["min"])
        report = {
            **injuries["rows"][0],
            "game_date": day,
            "matchup": "BOS@ATL" if sample["home"] else "ATL@BOS",
            "report_timestamp_utc": day + "T20:00:00Z",
            "injury_status": "Out" if sample["out"] else "Available",
        }
        reports.append(report)
        reports.extend(
            {**report, "player_id": j + 3, "injury_status": "Out"}
            for j in range(sample["other_reported_injury_out"])
        )
        panel.append(row)
    spec["end"] = panel[-1]["game_date"]
    summary, result = summarize_panel(panel), analyze(panel, reports)
    validate_evidence(spec, summary, result, panel, {"rows": reports}, plan)
    bundle = export(spec, summary, result)
    assert (
        bundle["statistics"]["adjusted"]["difference"]
        == result["primary"]["adjusted_difference"]
    )
    assert bundle["statistics"]["validated_significance"] is None
    result["primary"]["adjusted_difference"] = 999
    with pytest.raises(ValueError, match="Results do not match"):
        validate_evidence(spec, summary, result, panel, {"rows": reports}, plan)


def test_same_count_summary_from_another_study_is_rejected():
    spec, summary, result, panel, injuries, plan = evidence()
    summary["differences"]["ast"]["out_minus_participated"] = 999
    with pytest.raises(ValueError, match="Summary does not match"):
        validate_evidence(spec, summary, result, panel, injuries, plan)


def test_results_from_another_analysis_are_rejected():
    spec, summary, result, panel, injuries, plan = evidence()
    result["same_sample_raw_difference"] = 999
    with pytest.raises(ValueError, match="Results do not match"):
        validate_evidence(spec, summary, result, panel, injuries, plan)


def test_wrong_team_and_changed_plan_are_rejected():
    args = list(evidence())
    wrong = deepcopy(args)
    wrong[0]["team_abbr"] = "BOS"
    with pytest.raises(ValueError, match="spec and panel disagree"):
        validate_evidence(*wrong)
    args[-1]["primary_outcome"] = "pts"
    with pytest.raises(ValueError, match="Plan does not match"):
        validate_evidence(*args)


@pytest.mark.parametrize("changed", ["summary", "results", "panel"])
def test_cli_rejects_mixed_or_changed_artifacts_before_publication(
    tmp_path, monkeypatch, changed
):
    spec, summary, result, panel, injuries, plan = evidence()
    values = dict(
        spec=spec,
        summary=summary,
        results=result,
        panel=panel,
        injuries=injuries,
        plan=plan,
    )
    paths = {k: tmp_path / f"{k}.json" for k in values}
    for k, value in values.items():
        paths[k].write_text(json.dumps(value))
    manifest = {
        "inputs": {
            k: {
                "path": str(paths[k]),
                "sha256": hashlib.sha256(paths[k].read_bytes()).hexdigest(),
            }
            for k in ("panel", "injuries", "plan")
        }
    }
    paths["manifest"] = tmp_path / "manifest.json"
    paths["manifest"].write_text(json.dumps(manifest))
    if changed == "panel":
        values[changed][0]["outcomes"]["ast"] = 999
    elif changed == "summary":
        values[changed]["differences"]["ast"]["out_minus_participated"] = 999
    else:
        values[changed]["same_sample_raw_difference"] = 999
    paths[changed].write_text(json.dumps(values[changed]))
    output = tmp_path / "published.json"
    argv = ["export_teammate_study"]
    for k in ("spec", "summary", "results", "manifest"):
        argv.extend(["--" + k, str(paths[k])])
    argv.extend(["--output", str(output)])
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(ValueError, match="do(?:es)? not match|input changed"):
        main()
    assert not output.exists()
