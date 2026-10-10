"""Evaluation-only call budget and private usage log; no production retry changes."""

import json
from pathlib import Path
from threading import Lock
from time import monotonic
from types import SimpleNamespace


class CallBudget:
    def __init__(self, maximum, log_path=None):
        self.maximum = maximum
        self.calls = 0
        self.lock = Lock()
        self.records = []
        self.log_path = Path(log_path) if log_path else None

    def claim(self):
        with self.lock:
            if self.calls >= self.maximum:
                raise RuntimeError("Evaluation model-call budget exhausted")
            self.calls += 1
            return self.calls

    def record(self, record, response=None):
        with self.lock:
            self.records.append(record)
            if self.log_path:
                with self.log_path.open("a") as stream:
                    stream.write(
                        json.dumps({**record, "response": response}, default=str) + "\n"
                    )


class BudgetClient:
    def __init__(self, wrapped, budget):
        self.wrapped = wrapped
        self.budget = budget
        self.responses = SimpleNamespace(create=self.create)

    def with_options(self, **kwargs):
        return BudgetClient(self.wrapped.with_options(**kwargs), self.budget)

    def create(self, **kwargs):
        number = self.budget.claim()
        started = monotonic()
        record = {
            "call": number,
            "model": kwargs.get("model"),
            "format": kwargs.get("text", {}).get("format", {}).get("name"),
        }
        try:
            result = self.wrapped.responses.create(**kwargs)
        except Exception as exc:
            record.update(
                error_type=type(exc).__name__,
                latency_ms=round((monotonic() - started) * 1000),
            )
            self.budget.record(record)
            raise
        usage = getattr(result, "usage", None)
        if usage is not None:
            record["usage"] = (
                usage.model_dump() if hasattr(usage, "model_dump") else vars(usage)
            )
        record.update(
            latency_ms=round((monotonic() - started) * 1000),
            response_id=getattr(result, "id", None),
        )
        raw = (
            result.model_dump()
            if hasattr(result, "model_dump")
            else {"output_text": getattr(result, "output_text", None)}
        )
        self.budget.record(record, raw)
        return result
