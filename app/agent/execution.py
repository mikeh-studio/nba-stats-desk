"""Request-local, cooperative budgets and a single owner for provider retries."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from functools import wraps
from math import isfinite
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
        if self.retry_limit is not None:
            return self.retry_limit
        prefix = "anthropic" if self.provider == "claude" else self.provider
        return max(0, getattr(self.settings, f"{prefix}_agent_max_retries"))


ACTIVE_STAGES: ContextVar[tuple[str, ...]] = ContextVar("ask_active_stages", default=())

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
    token = CURRENT.set(policy)
    try:
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
    try:
        yield
        checkpoint()
    except Exception:
        checkpoint()
        raise
    finally:
        ACTIVE_STAGES.reset(token)
        if policy and policy.trace and name not in active:
            timings = policy.trace.stage_latency_ms
            timings[name] = timings.get(name, 0.0) + (monotonic() - started) * 1000


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
        timeout = policy.timeout(timeout_seconds)
        configured = client
        extra = {}
        if hasattr(client, "with_options"):
            configured = client.with_options(timeout=timeout, max_retries=0)
        else:
            extra["timeout"] = timeout
        if policy.trace:
            policy.trace.model_calls += 1
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
