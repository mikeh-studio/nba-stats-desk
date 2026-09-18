"""Governed exposure, shooting, and production-rate context for Ask."""

from app.agent.semantics import aggregate, load_contract

CONTEXT_METRICS = (
    "min",
    "fga",
    "fg3a",
    "fta",
    "fg_pct",
    "fg3_pct",
    "ft_pct",
    "efg_pct",
    "ts_pct",
    "tov",
    "pts_per36",
    "ast_per36",
)


def context_metrics(current, previous, *, baseline_covered=True):
    contract = load_contract()
    result = []
    for key in CONTEXT_METRICS:
        metric = contract.metric(key)
        aggregation = "ratio" if metric.denominator else "average"
        now, before = [
            aggregate(rows, metric, aggregation) for rows in (current, previous)
        ]
        scale = 100 if metric.unit == "ratio" else 1
        unit = (
            "percent"
            if scale == 100
            else "per 36 minutes"
            if metric.unit == "per36"
            else "minutes per game"
            if metric.unit == "minutes"
            else "per game"
        )
        change = (
            (now["value"] - before["value"]) * scale
            if baseline_covered
            and now["value"] is not None
            and before["value"] is not None
            and not now["missing_component_games"]
            and not before["missing_component_games"]
            else None
        )
        # Keep full precision and components in the audit; expose explicit units.
        result.append(
            {
                "key": key,
                "label": metric.label,
                **now,
                "value": now["value"] * scale if now["value"] is not None else None,
                "previous": {
                    **before,
                    "value": before["value"] * scale
                    if before["value"] is not None
                    else None,
                },
                "change": change,
                "unit": unit,
                "change_unit": "percentage points" if scale == 100 else unit,
                "definition": f"({metric.numerator}) / ({metric.denominator or 'valid appearances'})"
                + (" * 100" if scale == 100 else ""),
            }
        )
    return result


def context_table(metrics):
    def display(value):
        return "Unavailable" if value is None else f"{value:.1f}"

    return {
        "title": "Minutes, shooting, and production rates",
        "columns": [
            {"key": k, "label": label}
            for k, label in (
                ("metric", "Metric"),
                ("previous", "Comparison"),
                ("current", "Current"),
                ("change", "Change"),
                ("unit", "Unit / change unit"),
                ("coverage", "Valid / played games"),
            )
        ],
        "rows": [
            [
                m["label"],
                display(m["previous"]["value"]),
                display(m["value"]),
                display(m["change"]),
                f"{m['unit']} / {m['change_unit']}",
                f"{m['valid_games']} / {m['observed_games']}",
            ]
            for m in metrics
        ],
    }
