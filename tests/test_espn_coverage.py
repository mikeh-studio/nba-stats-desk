import copy

from scripts.espn_corrections import normalize
from scripts.espn_coverage import audit, check_players
from scripts.espn_sources import scoreboard
from tests.support.espn import DAY, SEASON, fixtures


def fixture():
    schedule, payload = fixtures()
    game = scoreboard(schedule, SEASON, DAY)[0]
    candidate = normalize(payload, game, "box")
    mappings = [
        {"source_player_id": p["source_player_id"], "nba_player_id": str(i + 10)}
        for i, p in enumerate(candidate["players"])
        if p["played"]
    ]
    check = check_players(candidate, mappings)
    report = {
        "season": SEASON,
        "first_date": str(DAY),
        "last_date": str(DAY),
        "status": "success",
        "discovery": [
            {
                "date": str(DAY),
                "status": "captured",
                "finals": 1,
                "game_ids": [game["source_game_id"]],
            }
        ],
        "games": [
            {
                "source_game_id": game["source_game_id"],
                "layers": [
                    {"layer": layer, "status": "published"}
                    for layer in ("result", "box")
                ],
                "mappings_status": "published",
                "player_check": check,
            }
        ],
    }
    return candidate, mappings, report


def test_complete_listed_sample_and_missing_minutes_only_block_dependent_metrics():
    candidate, mappings, report = fixture()
    candidate["players"][0]["stats"]["MIN"] = "--"
    report["games"][0]["player_check"] = check_players(candidate, mappings)
    result = audit(report, str(DAY))
    assert result["status"] == "passed"
    assert result["expected_final_games"] == result["accepted_games"] == 1
    assert result["metric_checks"]["pts"]["coverage_ready"]
    assert not result["metric_checks"]["pts_per36"]["coverage_ready"]
    assert not result["metric_checks"]["ast_per36"]["coverage_ready"]
    assert result["metric_checks"]["min"]["missing_input_rows"] == 1
    assert result["rankings_enabled"] is False


def test_zero_point_player_loss_unmapped_rows_and_duplicate_destinations_are_explicit():
    candidate, mappings, report = fixture()
    zero = next(p for p in candidate["players"] if p["stats"]["PTS"] == 0)
    candidate["players"].remove(zero)
    check = check_players(candidate, mappings)
    assert check["dropped_rows"] == 1
    report["games"][0]["player_check"] = check
    assert "dropped_rows" in audit(report, str(DAY))["issues"]
    candidate, mappings, _ = fixture()
    assert check_players(candidate, mappings[:-1])["unresolved_rows"] == 1
    mappings[0]["nba_player_id"] = mappings[1]["nba_player_id"]
    assert check_players(candidate, mappings)["duplicate_rows"] == 1


def test_missing_box_and_failed_or_unknown_date_never_become_empty_success():
    _, _, report = fixture()
    report["games"] = []
    result = audit(report, str(DAY))
    assert result["missing_games"] == ["1234567"]
    assert result["games_checked_through"] is None
    report["discovery"] = [{"date": str(DAY), "status": "failed"}]
    assert audit(report, str(DAY))["missing_dates"] == [str(DAY)]
    report["discovery"] = [{"date": str(DAY), "status": "captured", "finals": 0}]
    assert audit(report, str(DAY))["status"] == "blocked"
    report["discovery"][0]["game_ids"] = []
    assert audit(report, str(DAY))["status"] == "passed"


def test_prior_dates_are_required_and_failed_refresh_retains_previous_cutoff():
    _, _, report = fixture()
    initial = audit(report, str(DAY))
    later = copy.deepcopy(report)
    later["first_date"] = later["last_date"] = "2025-10-23"
    later["discovery"] = [
        {"date": "2025-10-23", "status": "captured", "finals": 0, "game_ids": []}
    ]
    later["games"] = []
    assert audit(later, str(DAY))["status"] == "blocked"
    complete = audit(later, str(DAY), initial)
    assert complete["games_checked_through"] == "2025-10-23"
    later["status"] = "failed"
    failed = audit(later, str(DAY), initial)
    assert failed["games_checked_through"] == str(DAY)
    assert not any(m["coverage_ready"] for m in failed["metric_checks"].values())


def test_same_game_on_two_dates_blocks_coverage():
    _, _, report = fixture()
    report["last_date"] = "2025-10-23"
    report["discovery"].append({**report["discovery"][0], "date": "2025-10-23"})
    assert audit(report, str(DAY))["duplicate_games"] == ["1234567"]


def test_weekly_record_carries_summary_and_detects_publication_row_loss(tmp_path):
    import json

    from scripts.weekly_refresh import record_coverage

    _, _, report = fixture()
    directory = tmp_path / "evidence/run"
    directory.mkdir(parents=True)
    (directory / "result.json").write_text(json.dumps(report))
    record = {
        "id": "run",
        "kind": "refresh",
        "observed_scope": {"groups": [{"season": SEASON, "row_count": 2}]},
    }
    state = {}
    record_coverage(
        {"source": "espn", "season": SEASON, "first_date": str(DAY)},
        tmp_path,
        record,
        state,
    )
    checks = state["coverage_checks"]
    assert checks["status"] == "blocked"
    assert "published_row_count_mismatch" in checks["issues"]
    assert "days" not in checks and "identities" not in checks
    assert record["coverage_checks"] == checks
    assert (tmp_path / "coverage.json").exists()
