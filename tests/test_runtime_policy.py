"""Actual OpenAI SDK serialization against an offline HTTP transport."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import replace
from time import monotonic

import httpx
import pytest
from app.agent import intelligence_contract as api
from app.agent.execution import ExecutionFailure, request_policy
from app.agent.intelligence import ModelGateway
from app.agent.runtime_policy import (
    CURRENT_ADMISSION,
    RuntimeAdmission,
    RuntimePolicy,
    guard_client,
    runtime_policy_scope,
)
from openai import OpenAI
from tests.support.semantic import settings


class Authority:
    def __init__(self, maximum=10):
        from threading import Lock

        self.lock = Lock()
        self.maximum = maximum
        self.events = []
        self.attempts = 0

    def admit(self, **kwargs):
        self.events.append(("admit", kwargs))
        return len(self.events)

    def reserve_attempt(self, admission, **kwargs):
        with self.lock:
            if self.attempts >= self.maximum:
                raise ExecutionFailure("budget_exhausted")
            self.attempts += 1
            self.events.append(("reserve", kwargs))

    def finish(self, admission, **kwargs):
        self.events.append(("finish", kwargs))


def policy(authority=None, **kwargs):
    return RuntimePolicy(
        authority or Authority(),
        max_model_calls=2,
        max_output_tokens=117,
        model="gpt-5.6-luna",
        **kwargs,
    )


def sdk(handler):
    return OpenAI(
        api_key="synthetic-key",
        max_retries=5,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def response(text="{}"):
    return {
        "id": "resp_fixture",
        "object": "response",
        "created_at": 0,
        "status": "completed",
        "model": "gpt-5.6-luna",
        "output": [
            {
                "id": "msg_fixture",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ],
    }


def invoke(client, config=None):
    with request_policy(config or settings(), "openai"):
        gateway = ModelGateway(
            client, "gpt-5.6-luna", api.PlanningBudget(monotonic() + 10, 9, 9000)
        )
        return asyncio.run(
            gateway.generate_plan(system_prompt="synthetic", user_payload="{}")
        )


@pytest.mark.parametrize("failure", [None, 429, 503, "timeout", "connection"])
def test_reserve_before_real_sdk_http_and_no_retries(failure):
    authority = Authority()
    runtime = policy(authority)
    wire = []

    def http(request):
        assert authority.attempts == 1
        data = json.loads(request.content)
        wire.append(data)
        assert data["max_output_tokens"] == 117
        assert data["store"] is False and data["tools"] == []
        assert str(request.url) == "https://api.openai.com/v1/responses"
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic", request=request)
        if failure == "connection":
            raise httpx.ConnectError("synthetic", request=request)
        return httpx.Response(
            failure or 200,
            json={"error": {"message": "synthetic"}} if failure else response(),
        )

    with runtime_policy_scope(runtime):
        token = CURRENT_ADMISSION.set(RuntimeAdmission(runtime, "case"))
        try:
            if failure:
                with pytest.raises(api.GatewayError):
                    invoke(sdk(http))
            else:
                assert invoke(sdk(http)) == "{}"
        finally:
            CURRENT_ADMISSION.reset(token)
    assert len(wire) == authority.attempts == 1


def test_clones_and_copied_worker_context_share_authority():
    authority = Authority(maximum=1)
    runtime = policy(authority)
    sent = []
    client = sdk(
        lambda request: sent.append(request) or httpx.Response(200, json=response())
    )
    with runtime_policy_scope(runtime):
        token = CURRENT_ADMISSION.set(RuntimeAdmission(runtime, "fixed-case"))
        try:
            guarded = guard_client(client).with_options(timeout=2, max_retries=0)
            contexts = [copy_context(), copy_context()]

            def call(context):
                try:
                    return context.run(invoke, guarded)
                except api.GatewayError:
                    return "denied"

            with ThreadPoolExecutor(2) as pool:
                assert sorted(pool.map(call, contexts)) == ["denied", "{}"]
        finally:
            CURRENT_ADMISSION.reset(token)
    assert len(sent) == authority.attempts == 1


def test_missing_admission_alternate_provider_endpoint_and_cancel_send_nothing():
    sent = []
    runtime = policy()
    client = sdk(
        lambda request: sent.append(request) or httpx.Response(200, json=response())
    )
    with runtime_policy_scope(runtime):
        with pytest.raises(api.GatewayError):
            invoke(client)
        with pytest.raises(ExecutionFailure):
            guard_client(client, "claude")
        with pytest.raises(ExecutionFailure):
            guard_client(client.with_options(base_url="https://example.invalid/v1"))
        admission = RuntimeAdmission(runtime, "case")
        admission.cancelled.set()
        token = CURRENT_ADMISSION.set(admission)
        try:
            with pytest.raises(api.GatewayError):
                invoke(client)
        finally:
            CURRENT_ADMISSION.reset(token)
    assert sent == []


def test_direct_guarded_call_cannot_increase_cap_or_enable_tools():
    sent = []
    runtime = policy()
    client = sdk(
        lambda request: (
            sent.append(json.loads(request.content))
            or httpx.Response(200, json=response())
        )
    )
    with runtime_policy_scope(runtime):
        token = CURRENT_ADMISSION.set(RuntimeAdmission(runtime, "case"))
        try:
            guarded = guard_client(client)
            kwargs = dict(
                model=runtime.model,
                store=False,
                max_output_tokens=9999,
                text={
                    "format": {
                        "name": "semantic_intent_v3",
                        "type": "json_schema",
                        "strict": True,
                        "schema": {"type": "object"},
                    }
                },
            )
            with pytest.raises(ExecutionFailure):
                guarded.responses.create(**kwargs, tools=[{"type": "web_search"}])
            with pytest.raises(ExecutionFailure):
                guarded.responses.create(**kwargs)
            invoke(guarded)
        finally:
            CURRENT_ADMISSION.reset(token)
    assert len(sent) == 1 and sent[0]["max_output_tokens"] == 117


def test_default_execution_retains_configured_retry_policy():
    with request_policy(
        replace(settings(), openai_agent_max_retries=4), "openai"
    ) as active:
        assert active.retries == 4


def test_raw_sdk_access_after_guard_and_redirect_cannot_bypass_wire_permit():
    from openai import APIConnectionError

    for redirect in (False, True):
        sent = []
        runtime = policy()

        def http(request):
            sent.append(request)
            return (
                httpx.Response(
                    307, headers={"location": "https://example.invalid/other"}
                )
                if redirect
                else httpx.Response(200, json=response())
            )

        client = sdk(http)
        with runtime_policy_scope(runtime):
            guard_client(client)
            # Original raw SDK handle shares the guard even outside the gateway.
            with pytest.raises(APIConnectionError):
                client.with_options(max_retries=0).responses.create(
                    model=runtime.model, input="synthetic"
                )
            assert sent == [] and runtime.authority.attempts == 0
            if redirect:
                token = CURRENT_ADMISSION.set(RuntimeAdmission(runtime, "case"))
                try:
                    with pytest.raises(api.GatewayError):
                        invoke(client)
                finally:
                    CURRENT_ADMISSION.reset(token)
                assert len(sent) == runtime.authority.attempts == 1


def test_raw_observer_contains_only_bounded_metadata():
    observed = []
    runtime = policy(on_wire_attempt=observed.append)
    client = sdk(lambda request: httpx.Response(200, json=response()))
    with runtime_policy_scope(runtime):
        token = CURRENT_ADMISSION.set(RuntimeAdmission(runtime, "case"))
        try:
            invoke(client)
        finally:
            CURRENT_ADMISSION.reset(token)
    assert observed == [{"model": runtime.model, "max_output_tokens": 117}]
