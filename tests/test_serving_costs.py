from scripts.ask_eval.costs import estimate_request, summarize_workload

PRICES = dict(
    model="fixture",
    input_per_million=1,
    cached_input_per_million=0.1,
    output_per_million=2,
    bigquery_per_tib=1,
    bigquery_billing="on_demand",
)


def test_request_cost_includes_failed_reads_and_no_double_reasoning():
    trace = dict(
        model_calls=1,
        models_used={"fixture": 1},
        data_read_coverage="ask_warehouse_reads/2",
        usage_reported_calls=1,
        token_detail_calls={"cached_input_tokens": 1},
        tokens={
            "prompt": 100,
            "cached_input": 80,
            "completion": 20,
            "reasoning_output": 10,
        },
        data_reads=[dict(kind="warehouse", status="error", bytes_billed=1024**4)],
    )
    result = estimate_request(trace, PRICES)
    assert result["model_estimate"] == 68 / 1_000_000
    assert result["bigquery_on_demand_estimate"] == 1
    trace["data_reads"][0]["bytes_billed"] = None
    assert not estimate_request(trace, PRICES)["complete"]


def test_missing_usage_is_unknown_and_refresh_cannot_be_ignored():
    assert estimate_request(dict(model_calls=1), PRICES)["model_estimate"] is None
    assert (
        summarize_workload(
            [dict(variable_request_estimate=1, success=True)],
            refresh_cost=None,
            storage_cost=0,
            hosting_cost=0,
        )["total_cost"]
        is None
    )
    result = summarize_workload(
        [
            dict(variable_request_estimate=1, success=True),
            dict(variable_request_estimate=2, success=False),
        ],
        refresh_cost=4,
        storage_cost=0,
        hosting_cost=0,
    )
    assert result["cost_per_success"] == 7


def test_capacity_allocation_and_inconsistent_zero_calls_are_unknown():
    assert (
        estimate_request(dict(model_calls=0, tokens={"prompt": 1}), PRICES)[
            "model_estimate"
        ]
        is None
    )
    assert not summarize_workload(
        [dict(variable_request_estimate=0, success=True)],
        refresh_cost=0,
        storage_cost=0,
        hosting_cost=0,
        bigquery_billing="capacity",
    )["complete"]
