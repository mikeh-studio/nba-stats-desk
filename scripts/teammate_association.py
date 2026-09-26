"""Exploratory adjustment for a frozen teammate panel; never causal inference."""

from collections import Counter, defaultdict
from statistics import mean

import numpy as np
import statsmodels.api as sm
from app.agent.teammate_readiness import latest_report, utc

COVARIATES = (
    "opponent_prior_win_pct",
    "rest_days",
    "home",
    "other_reported_injury_out",
)


def report_burden(game, reports, max_age=48):
    """Count injury-related Out listings in the latest captured team/game bulletin.

    This measures reported burden, not all roster absences. Unlisted is not healthy.
    Never combine old individual listings with a newer bulletin.
    """
    if not game.get("scheduled_start_utc") or game.get("postponed"):
        return {"value": None, "reason": "missing_usable_start"}
    matchup = (
        f"{game['opponent_abbr']}@{game['team_abbr']}"
        if game["home_away"] == "home"
        else f"{game['team_abbr']}@{game['opponent_abbr']}"
    )
    candidates = []
    for r in reports:
        if (
            r.get("season"),
            r.get("game_date"),
            r.get("team_abbr"),
            r.get("matchup"),
        ) != (game["season"], game["game_date"], game["team_abbr"], matchup):
            continue
        age = (
            utc(game["scheduled_start_utc"]) - utc(r["report_timestamp_utc"])
        ).total_seconds() / 3600
        if 0 < age <= max_age:
            if not r.get("source_url"):
                raise ValueError("Report source missing")
            candidates.append(r)
    if not candidates:
        return {"value": None, "reason": "no_eligible_team_report"}
    latest = max(utc(r["report_timestamp_utc"]) for r in candidates)
    selected = [r for r in candidates if utc(r["report_timestamp_utc"]) == latest]
    statuses = defaultdict(set)
    for r in selected:
        if r.get("player_id") is None:
            return {"value": None, "reason": "unresolved_player_in_bulletin"}
        if r["player_id"] in (game["player_id"], game["teammate_id"]):
            continue
        reason = r.get("reason") or ""
        if r["injury_status"] == "Out" and not reason:
            return {"value": None, "reason": "missing_out_reason"}
        statuses[r["player_id"]].add(
            (r["injury_status"], "injury/illness" in reason.lower())
        )
    if any(len(values) != 1 for values in statuses.values()):
        return {"value": None, "reason": "conflicting_bulletin"}
    return {
        "value": sum(("Out", True) in v for v in statuses.values()),
        "reported_at": latest.isoformat(),
        "source_urls": sorted({r["source_url"] for r in selected}),
    }


def prepare_rows(panel, reports):
    ages = [r.get("max_report_age_hours") for r in panel]
    if (
        not ages
        or any(
            isinstance(age, bool)
            or not isinstance(age, (int, float))
            or not 0 < age <= 48
            for age in ages
        )
        or len(set(ages)) != 1
    ):
        raise ValueError(
            "Panel requires one explicit report-age policy; rebuild older panels"
        )
    max_age = ages[0]
    keys = [
        (r["season"], r["game_id"], r["player_id"], r["teammate_id"]) for r in panel
    ]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate panel grain")
    if (
        len(
            {
                (r["season"], r["player_id"], r["teammate_id"], r["team_abbr"])
                for r in panel
            }
        )
        != 1
    ):
        raise ValueError("One pair/team/season required")
    result = []
    for r in sorted(panel, key=lambda r: (r["game_date"], r["game_id"])):
        if not r["included"]:
            continue
        if r["exposure"] not in ("participated", "reported_out_no_appearance"):
            raise ValueError("Included row has invalid exposure")
        status = latest_report(reports, r, r["teammate_id"], max_age)["status"]
        if (r["exposure"] == "reported_out_no_appearance" and status != "Out") or (
            r["exposure"] == "participated" and status in ("Out", "Conflicting")
        ):
            raise ValueError("Panel exposure conflicts with supplied reports")
        burden = report_burden(r, reports, max_age)
        result.append(
            {
                "game_id": r["game_id"],
                "game_date": r["game_date"],
                "month": r["game_date"][:7],
                "episode_id": r["episode_id"],
                "out": int(r["exposure"] == "reported_out_no_appearance"),
                **{
                    key: r["outcomes"].get(key)
                    for key in (
                        "pts",
                        "reb",
                        "ast",
                        "stl",
                        "blk",
                        "tov",
                        "fg3m",
                        "plus_minus",
                    )
                },
                "min": r["outcomes"].get("min"),
                "opponent_prior_win_pct": r.get("opponent_prior_win_pct"),
                "rest_days": r.get("rest_days"),
                "home": 1
                if r["home_away"] == "home"
                else 0
                if r["home_away"] == "away"
                else None,
                "other_reported_injury_out": burden["value"],
                "burden_evidence": burden,
            }
        )
    return result


def fit(rows, outcome="ast"):
    """Fixed specification; rank problems fail explicitly instead of a pseudoinverse fit."""
    if not rows or {r["out"] for r in rows} != {0, 1}:
        return {"status": "not_estimable", "reason": "both_exposures_required"}
    months = sorted({r["month"] for r in rows})
    names = ["intercept", "out", *COVARIATES, *[f"month:{m}" for m in months[1:]]]
    x = np.array(
        [
            [
                1,
                r["out"],
                *[r[c] for c in COVARIATES],
                *[int(r["month"] == m) for m in months[1:]],
            ]
            for r in rows
        ],
        dtype=float,
    )
    y = np.array(
        [36 * r["ast"] / r["min"] if outcome == "per36" else r[outcome] for r in rows],
        dtype=float,
    )
    if not np.isfinite(x).all() or not np.isfinite(y).all():
        raise ValueError("Non-finite analysis values")
    if len(rows) <= len(names) + 1 or np.linalg.matrix_rank(x) != len(names):
        return {
            "status": "not_estimable",
            "reason": "insufficient_residual_df_or_rank",
            "n": len(rows),
            "parameters": len(names),
        }
    model = sm.OLS(y, x).fit()
    diagnostics = {}
    for method in ("HC3", "cluster"):
        ids = [r["episode_id"] for r in rows]
        if method == "cluster" and len(set(ids)) < 2:
            diagnostics[method] = {"status": "not_estimable"}
            continue
        kwargs = (
            {"groups": ids, "use_correction": True, "df_correction": True}
            if method == "cluster"
            else {}
        )
        robust = model.get_robustcov_results(cov_type=method, use_t=True, **kwargs)
        interval = robust.conf_int()[1]
        values = [*interval, robust.pvalues[1]]
        diagnostics[method] = (
            {"nominal_95_ci": interval.tolist(), "nominal_p": float(robust.pvalues[1])}
            if np.isfinite(values).all()
            else {"status": "nonfinite_uncertainty"}
        )
    counts = Counter(r["episode_id"] for r in rows)
    return {
        "status": "estimated_exploratory",
        "n": len(rows),
        "group_n": dict(Counter(str(r["out"]) for r in rows)),
        "parameters": names,
        "adjusted_difference": float(model.params[1]),
        "nominal_uncertainty": diagnostics,
        "episodes": len(counts),
        "largest_episode_share": max(counts.values()) / len(rows),
        "max_leverage": float(model.get_influence().hat_matrix_diag.max()),
        "validated_significance": None,
    }


def analyze(panel, reports, outcome="ast"):
    if outcome not in (
        "pts",
        "reb",
        "ast",
        "stl",
        "blk",
        "tov",
        "fg3m",
        "min",
        "plus_minus",
    ):
        raise ValueError("Unsupported study outcome")
    prepared = prepare_rows(panel, reports)
    required = [outcome, *COVARIATES]
    missing = Counter()
    rows, excluded = [], []
    for r in prepared:
        absent = [c for c in required if r[c] is None]
        if absent:
            missing.update(absent)
            excluded.append({"game_id": r["game_id"], "missing": absent})
        else:
            rows.append(r)
    month_counts = defaultdict(Counter)
    for r in rows:
        month_counts[r["month"]][str(r["out"])] += 1
    overlap = {m for m, counts in month_counts.items() if counts["0"] and counts["1"]}
    common = [r for r in rows if r["month"] in overlap]
    balance = {}
    for c in COVARIATES:
        groups = [[r[c] for r in rows if r["out"] == g] for g in (0, 1)]
        balance[c] = {
            "played_mean": mean(groups[0]) if groups[0] else None,
            "out_mean": mean(groups[1]) if groups[1] else None,
            "ranges": [[min(v), max(v)] if v else None for v in groups],
        }
    differences = [
        mean([r[outcome] for r in rows if r["out"] == g])
        if any(r["out"] == g for r in rows)
        else None
        for g in (0, 1)
    ]
    primary = fit(rows, outcome=outcome)
    omissions = [
        {
            "omitted_episode": e,
            **fit([r for r in rows if r["episode_id"] != e], outcome=outcome),
        }
        for e in sorted({r["episode_id"] for r in rows})
    ]
    secondary = fit(
        [r for r in rows if r["min"] is not None and r["min"] > 0], outcome="per36"
    )
    return {
        "claim_level": "exploratory_adjusted_association",
        "validated_significance": None,
        "prepared_rows": prepared,
        "complete_case_game_ids": [r["game_id"] for r in rows],
        "excluded": excluded,
        "missing_counts": dict(missing),
        "month_exposure_counts": dict(month_counts),
        "covariate_balance": balance,
        "same_sample_raw_difference": differences[1] - differences[0]
        if None not in differences
        else None,
        "primary": primary,
        "overlap_months": sorted(overlap),
        "overlap_only": fit(common, outcome=outcome),
        "leave_episode_out": omissions,
        "secondary_mean_game_ast_per36": secondary,
        "limits": [
            "Nominal intervals/p-values are model diagnostics, not validated significance or causality.",
            "Few uneven exposure episodes; repeated games are not independent experiments.",
            "Complete-case exclusion changes the target sample; calendar overlap can be limited.",
            "Other reported injury Out counts are a proxy, not complete roster availability.",
            "No adjustment for unmeasured own health, coaching, shared-court minutes or within-month trends.",
            "Primary is assists per focal appearance. Same-game minutes are not a control; per36 is a secondary mean of game rates.",
            "Specification frozen after descriptive results were known; exploratory, not prospectively preregistered.",
        ],
    }
