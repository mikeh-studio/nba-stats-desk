"""Price-injected cost accounting; missing usage is unknown, never zero.

Rates and their source/as-of belong in private benchmark inputs. Capacity-billed
BigQuery runs must not also be represented as on-demand query spending.
"""

from __future__ import annotations

from math import isfinite


def estimate_request(trace: dict, prices: dict) -> dict:
    def rate(key):
        value = prices.get(key)
        return (
            value
            if type(value) in (int, float) and isfinite(value) and value >= 0
            else None
        )

    tokens = trace.get("tokens", {})
    attempts = trace.get("model_calls")
    reporting = trace.get("usage_reported_calls", 0)
    coverage = trace.get("token_detail_calls", {})
    model = None
    if (
        type(attempts) is int
        and attempts == 0
        and reporting == 0
        and not trace.get("models_used")
        and all(v in (None, 0) for v in tokens.values())
    ):
        model = 0.0
    elif (
        not trace.get("models_used_dropped")
        and trace.get("models_used") == {prices.get("model"): attempts}
        and prices.get("model") not in (None, "unknown")
        and reporting == attempts
        and (
            not str(trace.get("model", "")).startswith("claude")
            or coverage.get("cache_creation_input_tokens") == reporting
        )
        and all(coverage.get(k, 0) == reporting for k in ("cached_input_tokens",))
    ):
        cached = tokens.get("cached_input")
        created = tokens.get("cache_creation_input") or 0
        prompt = tokens.get("prompt")
        completion = tokens.get("completion")
        rates = [
            rate(k)
            for k in (
                "input_per_million",
                "cached_input_per_million",
                "output_per_million",
            )
        ]
        if created:
            rates.append(rate("cache_creation_per_million"))
        if (
            all(
                type(v) is int and v >= 0 for v in (prompt, completion, cached, created)
            )
            and prompt >= cached + created
            and all(r is not None for r in rates)
        ):
            model = (
                (prompt - cached - created) * rates[0]
                + cached * rates[1]
                + completion * rates[2]
                + (created * rates[3] if created else 0)
            ) / 1_000_000
    reads = [r for r in trace.get("data_reads", []) if r.get("kind") == "warehouse"]
    billed = [r.get("bytes_billed") for r in reads]
    warehouse = None
    if (
        trace.get("data_read_coverage") == "ask_warehouse_reads/2"
        and not trace.get("data_reads_dropped")
        and all(type(b) is int and b >= 0 for b in billed)
    ):
        if (
            prices.get("bigquery_billing") == "on_demand"
            and rate("bigquery_per_tib") is not None
        ):
            warehouse = sum(billed) * rate("bigquery_per_tib") / (1024**4)
        elif prices.get("bigquery_billing") == "capacity":
            warehouse = (
                0.0  # marginal on-demand spend only; allocate capacity separately
            )
    return dict(
        model_estimate=model,
        bigquery_on_demand_estimate=warehouse,
        variable_request_estimate=model + warehouse
        if model is not None and warehouse is not None
        else None,
        warehouse_attempts=len(reads),
        bytes_billed=sum(billed) if warehouse is not None else None,
        complete=model is not None and warehouse is not None,
        excludes=["refresh", "storage", "hosting", "capacity_allocation"],
        rate_source=prices.get("source"),
        rate_as_of=prices.get("as_of"),
    )


def summarize_workload(
    requests: list[dict],
    *,
    refresh_cost: float | None,
    storage_cost: float | None,
    hosting_cost: float | None,
    capacity_cost: float | None = None,
    bigquery_billing: str = "on_demand",
):
    if bigquery_billing not in ("on_demand", "capacity"):
        raise ValueError("Unknown BigQuery billing mode")
    allocated = 0.0 if bigquery_billing == "on_demand" else capacity_cost
    costs = [r.get("variable_request_estimate") for r in requests]
    success = sum(r.get("success") is True for r in requests)
    complete = bool(requests) and all(
        v is not None
        for v in costs + [refresh_cost, storage_cost, hosting_cost, allocated]
    )
    total = (
        sum(costs) + refresh_cost + storage_cost + hosting_cost + allocated
        if complete
        else None
    )
    return dict(
        attempts=len(requests),
        successes=success,
        total_cost=total,
        cost_per_success=total / success if total is not None and success else None,
        complete=complete,
    )
