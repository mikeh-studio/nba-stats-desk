"""Optional trusted process policy for a bounded, read-only Ask session.

This is installed by Python before application lifespan, never selected by HTTP.
The consumer owns durable admission and attempt accounting. Installed Python is
trusted code; this adapter is not a sandbox for malicious packages.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Event, Lock
from typing import Any, Callable, Protocol

from app.agent.execution import ExecutionFailure


class BudgetAuthority(Protocol):
    def admit(
        self, *, question: str, streaming: bool, provider: str, model: str
    ) -> Any:
        """Durably admit a fixed case, or fail closed (failed slots stay spent)."""
        ...

    def reserve_attempt(
        self, admission: Any, *, model: str, max_output_tokens: int
    ) -> None:
        """Durably reserve before send; failures and pending sends remain spent."""
        ...

    def finish(self, admission: Any, *, outcome: str) -> None: ...


@dataclass(frozen=True)
class RuntimePolicy:
    authority: BudgetAuthority
    max_model_calls: int
    max_output_tokens: int
    model: str
    source_policy: Any = None
    endpoint: str = "https://api.openai.com/v1"
    on_wire_attempt: Callable[[dict[str, Any]], None] | None = None

    def __post_init__(self):
        for value in (self.max_model_calls, self.max_output_tokens):
            if type(value) is not int or value <= 0:
                raise ValueError("Runtime limits must be positive integers")
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("Runtime model is required")
        if self.endpoint != "https://api.openai.com/v1":
            raise ValueError("Unsupported runtime endpoint")
        for name in ("admit", "reserve_attempt", "finish"):
            if not callable(getattr(self.authority, name, None)):
                raise ValueError("Runtime authority is incomplete")

    def validate_settings(self, settings):
        if (
            settings.agent_history_enabled
            or settings.agent_trace_enabled
            or settings.ask_local_espn_dev
            or settings.ask_serving_mode != "off"
        ):
            raise ValueError("Runtime policy forbids optional persistence and sources")

    def validate_selection(self, provider, model):
        if provider != "openai" or model != self.model:
            raise ExecutionFailure("runtime_selection_denied")


_PROCESS_POLICY: RuntimePolicy | None = None
_SCOPED_POLICY: ContextVar[RuntimePolicy | None] = ContextVar(
    "trusted_runtime_policy", default=None
)
AUTHORIZED_PLANNING_SCHEMA: ContextVar[str | None] = ContextVar(
    "authorized_planning_schema", default=None
)
CURRENT_ADMISSION: ContextVar[RuntimeAdmission | None] = ContextVar(
    "trusted_runtime_admission", default=None
)


def get_runtime_policy() -> RuntimePolicy | None:
    return _SCOPED_POLICY.get() or _PROCESS_POLICY


def configure_runtime_policy(policy: RuntimePolicy | None) -> None:
    """Trusted startup hook. Call before lifespan, with no running requests."""
    global _PROCESS_POLICY
    from app.agent.source_policy import configure_source_policy

    configure_source_policy(policy.source_policy if policy else None)
    _PROCESS_POLICY = policy


@contextmanager
def runtime_policy_scope(policy: RuntimePolicy):
    """Test/local assembly context; copied workers inherit the same authority."""
    from app.agent.source_policy import source_policy_scope

    token = _SCOPED_POLICY.set(policy)
    try:
        with source_policy_scope(policy.source_policy):
            yield policy
    finally:
        _SCOPED_POLICY.reset(token)


@dataclass
class RuntimeAdmission:
    policy: RuntimePolicy
    handle: Any
    cancelled: Event = field(default_factory=Event)
    lock: Any = field(default_factory=Lock)
    attempts: int = 0
    finished: bool = False
    worker_started: bool = False

    def reserve(self, model, tokens):
        with self.lock:
            if self.cancelled.is_set() or self.finished:
                raise ExecutionFailure("runtime_session_closed")
            if self.attempts >= self.policy.max_model_calls:
                raise ExecutionFailure("budget_exhausted")
            # Reserve failure is never converted into a provider retry.
            self.policy.authority.reserve_attempt(
                self.handle, model=model, max_output_tokens=tokens
            )
            self.attempts += 1

    def finish(self, outcome):
        with self.lock:
            if not self.finished:
                self.finished = True
                self.policy.authority.finish(self.handle, outcome=outcome)


def admit_runtime(*, question, streaming, provider, model):
    policy = get_runtime_policy()
    if policy is None:
        return None
    policy.validate_selection(provider, model)
    handle = policy.authority.admit(
        question=question, streaming=streaming, provider=provider, model=model
    )
    return RuntimeAdmission(policy, handle)


class _GuardedResponses:
    def __init__(self, owner):
        self.owner = owner

    def create(self, **kwargs):
        policy = self.owner.policy
        if get_runtime_policy() is not policy:
            raise ExecutionFailure("runtime_policy_missing")
        admission = CURRENT_ADMISSION.get()
        if admission is None or admission.policy is not policy:
            raise ExecutionFailure("runtime_admission_missing")
        policy.validate_selection("openai", kwargs.get("model"))
        if set(kwargs) - {
            "model",
            "instructions",
            "input",
            "text",
            "max_output_tokens",
            "store",
            "tools",
            "timeout",
        }:
            raise ExecutionFailure("runtime_operation_denied")
        schema = kwargs.get("text", {}).get("format", {})
        if (
            kwargs.get("tools")
            or kwargs.get("store") is not False
            or schema.get("name") != "semantic_intent_v3"
            or schema.get("type") != "json_schema"
            or schema.get("strict") is not True
            or set(schema) != {"name", "type", "strict", "schema"}
            or AUTHORIZED_PLANNING_SCHEMA.get() is None
            or json.dumps(schema.get("schema"), sort_keys=True)
            != AUTHORIZED_PLANNING_SCHEMA.get()
        ):
            raise ExecutionFailure("runtime_operation_denied")
        tokens = kwargs.get("max_output_tokens")
        if type(tokens) is not int or tokens <= 0:
            raise ExecutionFailure("runtime_operation_denied")
        tokens = min(tokens, policy.max_output_tokens)
        kwargs["max_output_tokens"] = tokens
        kwargs["tools"] = []
        permit = _WirePermit(
            admission,
            kwargs["model"],
            tokens,
            json.dumps(kwargs["text"], sort_keys=True),
        )
        token = _WIRE_PERMIT.set(permit)
        try:
            return self.owner.client.responses.create(**kwargs)
        finally:
            _WIRE_PERMIT.reset(token)


@dataclass
class _WirePermit:
    admission: RuntimeAdmission
    model: str
    tokens: int
    text_json: str
    sent: bool = False


_HTTP_HOOK_LOCK = Lock()

_WIRE_PERMIT: ContextVar[_WirePermit | None] = ContextVar(
    "runtime_wire_permit", default=None
)


def _runtime_http_guard(request):
    """Inspect actual SDK bytes immediately before the HTTP transport callback.

    A single-use permit also rejects raw SDK access, SDK retries and redirects.
    The hook lives on the shared HTTP client, so SDK clones cannot lose it.
    """
    runtime = get_runtime_policy()
    if runtime is None:
        return
    permit = _WIRE_PERMIT.get()
    if permit is None or permit.sent or permit.admission.policy is not runtime:
        raise ExecutionFailure("runtime_wire_denied")
    if request.method != "POST" or str(request.url) != runtime.endpoint + "/responses":
        raise ExecutionFailure("runtime_wire_denied")
    try:
        data = json.loads(request.content)
        valid = (
            data["model"] == permit.model == runtime.model
            and type(data["max_output_tokens"]) is int
            and data["max_output_tokens"] == permit.tokens <= runtime.max_output_tokens
            and data["store"] is False
            and data["tools"] == []
            and json.dumps(data["text"], sort_keys=True) == permit.text_json
        )
    except (ValueError, KeyError, TypeError):
        valid = False
    if not valid:
        raise ExecutionFailure("runtime_wire_denied")
    permit.sent = True
    permit.admission.reserve(permit.model, permit.tokens)
    if runtime.on_wire_attempt:
        runtime.on_wire_attempt(
            {"model": permit.model, "max_output_tokens": permit.tokens}
        )


class GuardedOpenAIClient:
    """Preserve the authority on SDK clones; SDK and application retries are off."""

    def __init__(self, client, policy):
        from openai import OpenAI

        if (
            not isinstance(client, OpenAI)
            or str(client.base_url).rstrip("/") != policy.endpoint
        ):
            raise ExecutionFailure("runtime_client_denied")
        self.policy = policy
        self.client = client.with_options(max_retries=0)
        with _HTTP_HOOK_LOCK:
            hooks = self.client._client.event_hooks["request"]
            if _runtime_http_guard not in hooks:
                hooks.append(_runtime_http_guard)
        self.responses = _GuardedResponses(self)

    def with_options(self, **kwargs):
        if (
            set(kwargs) - {"timeout", "max_retries"}
            or kwargs.get("max_retries", 0) != 0
        ):
            raise ExecutionFailure("runtime_client_denied")
        return GuardedOpenAIClient(self.client.with_options(**kwargs), self.policy)


def guard_client(client, provider="openai"):
    policy = get_runtime_policy()
    if policy is None:
        return client
    if provider != "openai":
        raise ExecutionFailure("runtime_selection_denied")
    if isinstance(client, GuardedOpenAIClient):
        if client.policy is not policy:
            raise ExecutionFailure("runtime_policy_missing")
        return client
    return GuardedOpenAIClient(client, policy)
