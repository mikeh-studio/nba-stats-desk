"""Request-local, cooperative budgets and a single owner for provider retries."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from functools import wraps
from math import isfinite
from threading import RLock
from time import monotonic, sleep
from typing import Any, Callable


class ExecutionFailure(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass
class RequestPolicy:
    settings: Any
    provider: str
    trace: Any = None
    deadline: float = 0.0
    on_success: list[Callable[[], None]] = field(default_factory=list)
    committing: bool = False
    retry_limit: int | None = None
    pending_clarifications: dict[tuple[int, str], Any] = field(default_factory=dict)
    warehouse_ledger: dict[str, int] = field(default_factory=lambda: {"bytes_used": 0})
    serving_state: dict[Any, dict[str, Any]] = field(default_factory=dict)
    warehouse_lock: Any = field(default_factory=RLock)
    warehouse_byte_limit: int = 100_000_000

    def remaining(self) -> float:
        remaining = self.deadline - monotonic()
        if remaining <= 0:
            raise ExecutionFailure("request_timeout")
        return remaining

    def timeout(self, cap: float | None = None) -> float:
        prefix = "anthropic" if self.provider == "claude" else self.provider
        configured = getattr(self.settings, f"{prefix}_agent_timeout_seconds")
        if (
            not isfinite(configured)
            or configured <= 0
            or (cap is not None and (not isfinite(cap) or cap <= 0))
        ):
            raise ValueError("Provider timeouts must be positive and finite")
        return min(configured, cap if cap is not None else configured, self.remaining())

    @property
    def retries(self) -> int:
        from app.agent.runtime_policy import get_runtime_policy

        if get_runtime_policy() is not None:
            return 0
        if self.retry_limit is not None:
            return self.retry_limit
        prefix = "anthropic" if self.provider == "claude" else self.provider
        return max(0, getattr(self.settings, f"{prefix}_agent_max_retries"))


ACTIVE_STAGES: ContextVar[tuple[str, ...]] = ContextVar("ask_active_stages", default=())

STAGE_CHILDREN: ContextVar[list[float] | None] = ContextVar(
    "ask_stage_children", default=None
)

MODEL_ATTEMPT: ContextVar[Callable[[], None] | None] = ContextVar(
    "model_attempt", default=None
)

CURRENT: ContextVar[RequestPolicy | None] = ContextVar("ask_execution", default=None)


@contextmanager
def request_policy(settings, provider, trace=None):
    if (
        not isfinite(settings.agent_request_deadline_seconds)
        or settings.agent_request_deadline_seconds <= 0
    ):
        raise ValueError("Request deadline must be positive and finite")
    policy = RequestPolicy(
        settings, provider, trace, monotonic() + settings.agent_request_deadline_seconds
    )
    from app.agent.formulas import formula_metadata_cache

    token = CURRENT.set(policy)
    try:
        with formula_metadata_cache(
            getattr(settings, "ask_formula_cache_enabled", False)
        ):
            policy.remaining()
            yield policy
    finally:
        CURRENT.reset(token)


@contextmanager
def optional_model_budget(seconds: float, reserve_seconds: float = 1.0):
    """Spend a bounded slice on enrichment, preserving time to deliver the answer."""
    policy = CURRENT.get()
    if policy is None:
        yield
        return
    allowance = min(seconds, policy.remaining() - reserve_seconds)
    if allowance <= 0:
        raise ExecutionFailure("optional_timeout")
    token = CURRENT.set(
        replace(policy, deadline=monotonic() + allowance, retry_limit=0)
    )
    try:
        yield
        checkpoint()
    finally:
        CURRENT.reset(token)


def defer_until_success(callback):
    policy = CURRENT.get()
    if policy and not policy.committing:
        policy.on_success.append(callback)
        return True
    return False


def checkpoint():
    policy = CURRENT.get()
    # The final checkpoint establishes success. Finish staged memory writes
    # after it, rather than allowing a deadline to interrupt half-way through.
    if policy and not policy.committing:
        policy.remaining()


def bounded_timeout(seconds):
    policy = CURRENT.get()
    return min(seconds, policy.remaining()) if policy else seconds


@contextmanager
def stage(name):
    policy = CURRENT.get()
    checkpoint()
    started = monotonic()
    active = ACTIVE_STAGES.get()
    token = ACTIVE_STAGES.set((*active, name))
    parent_children = STAGE_CHILDREN.get()
    children = [0.0]
    child_token = STAGE_CHILDREN.set(children)
    try:
        yield
        checkpoint()
    except Exception:
        checkpoint()
        raise
    finally:
        elapsed = (monotonic() - started) * 1000
        STAGE_CHILDREN.reset(child_token)
        if parent_children is not None:
            parent_children[0] += elapsed
        ACTIVE_STAGES.reset(token)
        if policy and policy.trace:
            exclusive = policy.trace.stage_exclusive_latency_ms
            exclusive[name] = exclusive.get(name, 0.0) + max(0.0, elapsed - children[0])
        if policy and policy.trace and name not in active:
            timings = policy.trace.stage_latency_ms
            timings[name] = timings.get(name, 0.0) + elapsed


def timed(name):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            with stage(name):
                return fn(*args, **kwargs)

        return wrapped

    return decorate


def governed_request(fn):
    @wraps(fn)
    def wrapped(self, *args, **kwargs):
        trace = kwargs.get("trace")
        with request_policy(
            self.settings, (kwargs.get("provider") or "openai").strip().lower(), trace
        ) as policy:
            try:
                result = fn(self, *args, **kwargs)
                checkpoint()
                policy.committing = True
                for callback in policy.on_success:
                    callback()
                if trace and trace.outcome == "unknown":
                    trace.outcome = result.get("status", "answered")
                return result
            except Exception as exc:
                cause = exc
                while cause.__cause__ is not None and not isinstance(
                    cause, ExecutionFailure
                ):
                    cause = cause.__cause__
                if trace and isinstance(cause, ExecutionFailure):
                    trace.outcome = cause.code
                    trace.error_type = cause.code
                raise

    return wrapped


def transient(exc):
    status = getattr(exc, "status_code", None)
    return (
        (isinstance(status, int) and (status in {408, 409, 429} or 500 <= status < 600))
        or isinstance(exc, (TimeoutError, ConnectionError))
        or type(exc).__name__ in {"APITimeoutError", "APIConnectionError"}
    )


def create_response(
    client, *, settings=None, provider="openai", timeout_seconds=None, **kwargs
):
    if CURRENT.get() is None:
        if settings is None:
            from app.config import get_settings

            settings = get_settings()
        with request_policy(settings, provider):
            return create_response(client, timeout_seconds=timeout_seconds, **kwargs)
    policy = CURRENT.get()
    assert policy is not None
    from app.agent.runtime_policy import get_runtime_policy, guard_client

    runtime = get_runtime_policy()
    if runtime is not None:
        runtime.validate_selection(policy.provider, kwargs.get("model"))
        client = guard_client(client, policy.provider)
    callback = kwargs.pop("on_output_text_delta", None)
    emitted = False

    def on_delta(delta):
        nonlocal emitted
        checkpoint()
        if delta:
            emitted = True
        if callback:
            callback(delta)

    # Consume Claude streams even without a user callback, checking the budget
    # between chunks. Only this adapter accepts the delta callback argument.
    if policy.provider == "claude":
        kwargs["on_output_text_delta"] = on_delta
    for attempt in range(policy.retries + 1):
        if before_attempt := MODEL_ATTEMPT.get():
            before_attempt()
        timeout = policy.timeout(timeout_seconds)
        configured = client
        extra = {}
        if hasattr(client, "with_options"):
            configured = client.with_options(timeout=timeout, max_retries=0)
        else:
            extra["timeout"] = timeout
        if policy.trace:
            policy.trace.model_calls += 1
            import re

            model_id = kwargs.get("model")
            if not isinstance(model_id, str) or not re.fullmatch(
                r"[A-Za-z0-9_.:/-]{1,120}", model_id
            ):
                model_id = "unknown"
            if (
                model_id in policy.trace.models_used
                or len(policy.trace.models_used) < 8
            ):
                policy.trace.models_used[model_id] = (
                    policy.trace.models_used.get(model_id, 0) + 1
                )
            else:
                policy.trace.models_used_dropped = True
        schema_name = kwargs.get("text", {}).get("format", {}).get("name", "")
        try:
            with stage(
                "planning"
                if schema_name
                in {
                    "research_scope",
                    "research_intent_v2",
                    "research_intent_v3",
                    "semantic_plan",
                    "nba_stats_query_plan",
                    "semantic_intent_v2",
                    "semantic_intent_v3",
                }
                else "generation"
            ):
                response = configured.responses.create(**kwargs, **extra)
            checkpoint()
            return response
        except ExecutionFailure:
            raise
        except Exception as exc:
            checkpoint()
            if policy.trace:
                policy.trace.provider_failures += 1
            if emitted or not transient(exc) or attempt == policy.retries:
                code = (
                    "provider_timeout"
                    if isinstance(exc, TimeoutError)
                    or type(exc).__name__ == "APITimeoutError"
                    else "provider_failure"
                )
                if (
                    isinstance(exc, ValueError)
                    or type(exc).__name__ == "ValidationError"
                ):
                    code = (
                        "invalid_plan"
                        if schema_name
                        in {
                            "research_scope",
                            "research_intent_v2",
                            "research_intent_v3",
                            "semantic_plan",
                            "nba_stats_query_plan",
                            "semantic_intent_v2",
                            "semantic_intent_v3",
                        }
                        else "invalid_response"
                    )
                raise ExecutionFailure(code) from exc
            delay = max(0.0, policy.settings.openai_agent_retry_base_delay_seconds) * (
                2**attempt
            )
            if delay >= policy.remaining():
                raise ExecutionFailure("request_timeout") from exc
            sleep(delay)
    raise AssertionError("unreachable")


def warehouse_budget(cap: int) -> int:
    policy = CURRENT.get()
    if policy is None:
        return cap
    remaining = policy.warehouse_byte_limit - policy.warehouse_ledger["bytes_used"]
    if remaining < 10 * 1024 * 1024:
        raise ExecutionFailure("query_budget_exceeded")
    return min(cap, remaining)


def record_data_read(
    *,
    kind: str,
    status: str,
    rows: int | None = None,
    bytes_processed: int | None = None,
    bytes_billed: int | None = None,
    cache_hit: bool | None = None,
    source_version: str | None = None,
    budget_ceiling: int | None = None,
    transferred_bytes: int | None = None,
):
    """Bounded metadata only: never SQL, identifiers, question text or row values."""
    import re

    policy = CURRENT.get()
    if policy is None:
        return
    if kind == "warehouse":
        charged = (
            bytes_billed
            if type(bytes_billed) is int and bytes_billed >= 0
            else budget_ceiling
        )
        policy.warehouse_ledger["bytes_used"] += (
            charged if type(charged) is int and charged >= 0 else 100_000_000
        )
    if policy.trace is None:
        return
    if kind not in {"warehouse", "evidence_cache"} or status not in {"ok", "error"}:
        raise ValueError("Invalid data-read telemetry category")
    record: dict[str, Any] = {"kind": kind, "status": status}
    for key, value in (
        ("rows", rows),
        ("bytes_processed", bytes_processed),
        ("bytes_billed", bytes_billed),
        ("transferred_bytes", transferred_bytes),
    ):
        record[key] = value if type(value) is int and value >= 0 else None
    record["cache_hit"] = cache_hit if type(cache_hit) is bool else None
    record["source_version"] = (
        source_version
        if isinstance(source_version, str)
        and re.fullmatch(r"[0-9a-f]{64}", source_version)
        else None
    )
    if len(policy.trace.data_reads) < 64:
        policy.trace.data_reads.append(record)
    else:
        policy.trace.data_reads_dropped += 1


def record_serving_shadow(outcome: dict[str, str]) -> None:
    """Safe, bounded candidate status, without identities, questions or values."""
    import re

    policy = CURRENT.get()
    if policy is None or policy.trace is None:
        return
    if outcome.get("status") not in {"match", "mismatch", "ineligible", "error"}:
        raise ValueError("Invalid shadow status")
    record = {"status": outcome["status"]}
    code = outcome.get("code")
    if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,64}", code):
        record["code"] = code
    if len(policy.trace.serving_shadow) < 64:
        policy.trace.serving_shadow.append(record)
    else:
        policy.trace.serving_shadow_dropped += 1


def warehouse_bytes_billed(job, failure: Exception | None = None) -> int | None:
    """Keep ambiguous transport/cancellation charges unknown; validation errors cost zero.

    A result-fetch failure after execution is not a failed SQL validation. Prefer
    reported billed bytes and only infer zero from definitive submission/job errors.
    https://cloud.google.com/bigquery/pricing
    """
    from google.api_core.exceptions import BadRequest, Forbidden, NotFound

    value = getattr(job, "total_bytes_billed", None)
    if type(value) is int and value >= 0:
        return value
    error = getattr(job, "error_result", None)
    if isinstance(error, dict) and error.get("reason") in {
        "notFound",
        "invalidQuery",
        "accessDenied",
        "billingTierLimitExceeded",
    }:
        return 0
    if job is None and isinstance(failure, (BadRequest, Forbidden, NotFound)):
        return 0
    return None


def serialized_warehouse_read(fn):
    """Share the request budget safely across repository worker threads.

    Only governed reads serialize. Outside Ask the existing parallel behavior is
    unchanged. A bounded queue wait ensures ceilings cannot race ahead of charges.
    """

    @wraps(fn)
    def wrapped(*args, **kwargs):
        policy = CURRENT.get()
        if policy is None:
            return fn(*args, **kwargs)
        acquire_timed_lock(policy.warehouse_lock, "warehouse_queue_wait", 120)
        try:
            return fn(*args, **kwargs)
        finally:
            policy.warehouse_lock.release()

    return wrapped


def acquire_timed_lock(lock, name, seconds=120):
    """An exit checkpoint must never strand a successfully acquired lock."""
    acquired = False
    try:
        with stage(name):
            acquired = lock.acquire(timeout=bounded_timeout(seconds))
        if not acquired:
            raise ExecutionFailure("request_timeout")
    except BaseException:
        if acquired:
            lock.release()
        raise


@contextmanager
def optional_shadow_budget(seconds=15.0):
    """Isolate optional execution while accounting all spend in the request cap."""
    policy = CURRENT.get()
    if policy is None:
        yield
        return
    acquire_timed_lock(policy.warehouse_lock, "warehouse_queue_wait")
    child = None
    token = None
    try:
        limit = warehouse_budget(50_000_000)
        allowance = min(seconds, policy.remaining() - 1.0)
        if allowance <= 0:
            raise ExecutionFailure("optional_timeout")
        child = replace(
            policy,
            deadline=monotonic() + allowance,
            retry_limit=0,
            warehouse_ledger={"bytes_used": 0},
            warehouse_byte_limit=limit,
        )
        token = CURRENT.set(child)
        yield
    finally:
        if token is not None:
            CURRENT.reset(token)
        if child is not None:
            policy.warehouse_ledger["bytes_used"] += child.warehouse_ledger[
                "bytes_used"
            ]
        policy.warehouse_lock.release()
