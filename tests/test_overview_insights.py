"""Independent basketball contrasts: opportunity, efficiency, peers and self."""

from copy import deepcopy

import pytest
from app.agent.overview_insights import (
    insight_paragraphs,
    peer_context,
    select_insights,
)


@pytest.fixture
def metrics():
    # Hand-calculated scenario: more minutes/attempts, lower TS%, higher PTS/36;
    # assists remain high versus peers while declining against own baseline.
    values = {
        "pts": (30, 5),
        "ast": (6, -1),
        "tov": (3, 1),
        "min": (36, 4),
        "fga": (24, 4),
        "fta": (8, 2),
        "ts_pct": (55, -2),
        "pts_per36": (30, 1.875),
        "ast_per36": (6, -1.875),
        "reb": (4, 0),
        "stl": (1, 0),
        "blk": (0, 0),
    }
    result = []
    for key, (value, change) in values.items():
        result.append(
            dict(
                key=key,
                label=key,
                value=value,
                change=change,
                display_change=change,
                missing_component_games=0,
                sample={"comparison_eligible": True, "status": "descriptive"},
            )
        )
    for m in result:
        if m["key"] in ("pts", "ast"):
            m.update(
                percentile=90,
                percentile_status="ranked",
                cohort_size=11,
                percentile_policy={
                    "version": "ask_overview_percentiles/1",
                    "min_games": 5,
                },
                distribution={
                    "among_players": {
                        "focal_in_cohort": True,
                        "summary": {"n": 11, "median": 4 if m["key"] == "ast" else 15},
                    }
                },
            )
    return result


def prose(metrics, profile="general"):
    return " ".join(
        insight_paragraphs(
            "Example",
            metrics,
            {"comparison_label": "the same season's regular season"},
            10,
            "playoff",
            profile,
        )
    )


def test_volume_efficiency_and_minutes_are_distinguished(metrics):
    text = prose(metrics)
    assert "True shooting was lower" in text
    assert "4.0 more field-goal attempts and 2.0 more free-throw attempts" in text
    assert "57.0% to 55.0%" in text
    assert "28.1 to 30.0" in text
    assert "extra playing time was only part" in text
    assert "caused" not in text


def test_strong_league_assists_can_coexist_with_decline_against_self(metrics):
    text = prose(metrics)
    assert "assists fell 1.0" in text and "Turnovers rose 1.0" in text
    assert "assists fell 1.0" in text and "Turnovers rose 1.0" in text
    assert "assists per game exceeded 90.0%" in text
    assert "11 qualifying playoff players with at least 5 appearances" in text
    assert "7.9 to 6.0" in text


def test_more_minutes_can_hide_lower_scoring_rate(metrics):
    next(m for m in metrics if m["key"] == "pts_per36")["display_change"] = -2
    assert "opposite direction to his per-game output" in prose(metrics)
    assert "extends beyond" not in prose(metrics)


def test_efficiency_gain_is_not_mislabeled_volume_only(metrics):
    next(m for m in metrics if m["key"] == "ts_pct")["display_change"] = 3
    assert "True shooting was higher" in prose(metrics)
    assert "not an efficiency gain" not in prose(metrics)


@pytest.mark.parametrize(
    "mutation", ["legacy", "excluded", "small_cohort", "mismatched", "missing"]
)
def test_peer_claim_requires_current_complete_qualification(metrics, mutation):
    m = metrics[0]
    if mutation == "legacy":
        m.pop("percentile_policy")
    elif mutation == "excluded":
        m["percentile_status"] = "low_attempts"
    elif mutation == "small_cohort":
        m["cohort_size"] = 1
    elif mutation == "mismatched":
        m["distribution"]["among_players"]["summary"]["n"] = 12
    else:
        m["missing_component_games"] = 1
    assert peer_context(m, "playoff") == ""


def test_stable_low_count_stats_are_not_forced_into_prose(metrics):
    text = prose(metrics)
    assert "blocks" not in text and "steals" not in text
    assert "positioning" not in text and "unavailable" not in text
    assert len(select_insights(metrics, {}, "playoff")) == 2


def test_material_rebounding_change_is_selected(metrics):
    next(m for m in metrics if m["key"] == "reb").update(value=8, display_change=4)
    assert "Rebounds rose 4.0" in prose(metrics)


def test_missing_baseline_does_not_reconstruct_raw_changes(metrics):
    for m in metrics:
        m["display_change"] = None
    text = prose(metrics)
    assert "rose" not in text and "fell" not in text and "baseline:" not in text
    assert "usual" not in text
    assert all(
        "usual" not in f["title"] for f in select_insights(metrics, {}, "playoff")
    )
    assert "90.0%" in text


def test_null_not_zero_and_reordered_evidence_is_identical(metrics):
    original = deepcopy(metrics)
    next(m for m in metrics if m["key"] == "tov").update(value=None)
    text = prose(metrics)
    assert "0.0 turnovers" not in text and "3.0 turnovers" not in text
    assert "6.0 assists" in text
    assert prose(list(reversed(metrics))) == text
    assert prose(original) != text


def test_contribution_stays_non_scoring(metrics):
    text = prose(metrics, "contribution")
    assert "assists per game exceeded 90.0%" in text
    assert "30.0 points" not in text and "True shooting" not in text


@pytest.mark.parametrize("delta", [-0.1, 0, 0.1, 0.49])
def test_small_efficiency_moves_use_mild_language(metrics, delta):
    next(m for m in metrics if m["key"] == "ts_pct")["display_change"] = delta
    text = prose(metrics)
    assert "True shooting was similar" in text
    assert "True shooting was lower" not in text
    assert "efficiency rose" not in select_insights(metrics, {}, "playoff")[0]["title"]


@pytest.mark.parametrize("a,d", [(-1, 0.75), (1, -0.75)])
def test_creation_surfaces_opposing_per_game_and_per_minute_directions(metrics, a, d):
    next(m for m in metrics if m["key"] == "ast")["display_change"] = a
    next(m for m in metrics if m["key"] == "ast_per36")["display_change"] = d
    finding = next(
        f for f in select_insights(metrics, {}, "playoff") if f["area"] == "Creation"
    )
    assert "opposite direction" in finding["text"]
    assert finding["title"] == "Assist volume and rate diverged"
    assert f"Assists per 36 {'rose' if d > 0 else 'fell'} 0.8" in finding["summary"]


@pytest.mark.parametrize(
    "pct,leader", [(100, True), (99.999, False), (90, False), (0, False)]
)
def test_leader_wording_uses_unrounded_strict_rank(metrics, pct, leader):
    metrics[0]["percentile"] = pct
    text = prose(metrics)
    assert ("led qualifying" in text) is leader
    assert "100.0%" not in text


def test_summary_scope_is_last_and_cohort_is_not_repeated(metrics):
    parts = insight_paragraphs(
        "Example", metrics, {"previous_appearances": 74}, 19, "playoff", "general"
    )
    assert not parts[0].startswith("Scope:")
    assert parts[-1].startswith("Scope: 19 playoff appearances")
    assert "74 appearances" in parts[-1]
    assert " ".join(parts).count("11 qualifying playoff players") == 1
    assert len(" ".join(parts[:-1]).split()) <= 150


def test_distinct_metric_cohorts_keep_their_own_qualifications(metrics):
    from app.agent.overview_insights import scope_context

    ts = next(m for m in metrics if m["key"] == "ts_pct")
    for key in (
        "percentile",
        "percentile_status",
        "cohort_size",
        "percentile_policy",
        "distribution",
    ):
        ts[key] = deepcopy(metrics[0][key])
    ts.update(cohort_size=8)
    ts["distribution"]["among_players"]["summary"]["n"] = 8
    ts["percentile_policy"].update(min_attempts=100, attempt_field="fga")
    context = scope_context(
        metrics, {}, 10, "playoff", [{"peer_keys": ["pts", "ts_pct"]}]
    )
    assert "points: 11 qualifying" in context
    assert "true shooting: 8 qualifying" in context
    assert "100 FGA" in context


def test_sidebar_supports_relationship_instead_of_first_clause(metrics):
    for f in select_insights(metrics, {}, "playoff"):
        assert f["summary"] != f["text"].split(". ")[0] + "."
    scoring, creation = select_insights(metrics, {}, "playoff")
    assert "57.0% to 55.0%" in scoring["summary"]
    assert "Assists fell 1.0" in creation["summary"]
    assert "turnovers rose 1.0" in creation["summary"]


def test_stable_elite_activity_is_retained_without_forcing_a_paragraph(metrics):
    reb = next(m for m in metrics if m["key"] == "reb")
    ref = metrics[0]
    for key in (
        "percentile",
        "percentile_status",
        "cohort_size",
        "percentile_policy",
        "distribution",
    ):
        reb[key] = deepcopy(ref[key])
    parts = insight_paragraphs("Example", metrics, {}, 10, "playoff", "general")
    assert len(parts) == 3  # two narrative paragraphs and compact scope
    assert "4.0 rebounds" in parts[1]
    assert len(select_insights(metrics, {}, "playoff")) == 3


def test_activity_priority_is_explicit_and_independent_of_input_order(metrics):
    for m in metrics:
        if m["key"] in ("reb", "stl", "blk"):
            m.update(value=5, display_change=2)
    findings = select_insights(metrics, {}, "playoff")
    assert findings[-1]["metric_keys"] == ["reb"]
    assert findings == select_insights(list(reversed(metrics)), {}, "playoff")


def test_focused_scoring_sidebar_retains_opposite_rate(metrics):
    next(m for m in metrics if m["key"] == "pts_per36")["display_change"] = -2
    findings = select_insights(metrics, {}, "playoff", "scoring")
    assert len(findings) == 1
    assert "opposite direction" in findings[0]["text"]
    assert "opposite direction" in findings[0]["summary"]
