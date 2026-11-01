"""Descriptive percentiles with explicit, versioned overview qualification rules."""

from app.agent.metric_distributions import distribution
from app.agent.semantics import aggregate, load_contract

ATTEMPT_MINIMUMS = {
    "fg_pct": 100,
    "ts_pct": 100,
    "efg_pct": 100,
    "fg3_pct": 50,
    "ft_pct": 50,
}
RANKED_CONTEXT = {"fg3m", "tov", "min", *ATTEMPT_MINIMUMS}
LABELS = {
    "unavailable": "No data",
    "incomplete_data": "Incomplete data",
    "insufficient_games": "Low sample",
    "low_attempts": "Low attempts",
    "too_few_peers": "Few peers",
    "not_ranked": "Not ranked",
}


def add_percentiles(metrics, grouped, player_id, minimum, *, own_rows=None):
    contract = load_contract()
    for record in metrics:
        key = record["key"]
        metric = contract.metric(key)
        threshold = ATTEMPT_MINIMUMS.get(key)
        ranked = key in {"pts", "reb", "ast", "stl", "blk"} | RANKED_CONTEXT

        def reason(stats):
            if stats["value"] is None:
                return "unavailable"
            if stats["missing_component_games"]:
                return "incomplete_data"
            if stats["valid_games"] < minimum:
                return "insufficient_games"
            if (
                threshold is not None
                and stats["components"].get(metric.attempt_field, 0) < threshold
            ):
                return "low_attempts"
            return None

        summaries = (
            {
                pid: aggregate(
                    rows, metric, "ratio" if metric.denominator else "average"
                )
                for pid, rows in grouped.items()
            }
            if ranked
            else {}
        )
        cohort = {pid: s for pid, s in summaries.items() if reason(s) is None}
        own = summaries.get(player_id, record)
        exclusion = reason(own) if ranked else "not_ranked"
        if exclusion is None and len(cohort) < 2:
            exclusion = "too_few_peers"
        pct = None
        if exclusion is None:
            value = own["value"]
            worse = sum(
                (
                    s["value"] > value
                    if metric.direction == "lower"
                    else s["value"] < value
                )
                for pid, s in cohort.items()
                if pid != player_id
            )
            pct = 100 * worse / (len(cohort) - 1)
        attempt_label = {"fg3a": "3PA", "fga": "FGA", "fta": "FTA"}.get(
            metric.attempt_field, ""
        )
        attempts = f"; at least {threshold} {attempt_label}" if threshold else ""
        meaning = (
            "Higher percentile means fewer turnovers; playing time and role matter."
            if key == "tov"
            else "Minutes describe playing-time volume, not performance quality."
            if key == "min"
            else "Descriptive rank, not a skill or impact grade."
        )
        text = (
            f"{len(cohort)} qualified players in the same dates and phases; at least {minimum} valid appearances{attempts}; complete metric components. Ties share the percentage of other qualified players strictly worse. "
            + meaning
        )
        if not ranked:
            text = "This overview does not rank this context metric. See its raw value and sample."
        if ranked:
            record["distribution"] = distribution(
                metric,
                cohort,
                own_rows if own_rows is not None else grouped.get(player_id, []),
                player_id,
            )
        record.update(
            percentile=pct,
            cohort_size=len(cohort),
            percentile_status="ranked" if pct is not None else exclusion,
            percentile_label=LABELS.get(exclusion),
            percentile_note=text,
            percentile_policy={
                "version": "ask_overview_percentiles/1",
                "min_games": minimum,
                "attempt_field": metric.attempt_field if threshold else None,
                "min_attempts": threshold,
                "min_cohort_size": 2,
                "direction": metric.direction,
            },
        )
