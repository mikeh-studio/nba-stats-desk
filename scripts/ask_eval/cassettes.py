"""Strict evaluation-only provider recordings; never an application fallback."""

from __future__ import annotations

import hashlib
import json
import sys
from contextvars import ContextVar
from pathlib import Path
from threading import Lock
from time import monotonic
from types import SimpleNamespace

from scripts.ask_eval.private_io import private_directory, private_open

VERSION = 2
SUPPORTED_VERSIONS = (1, VERSION)
call_context: ContextVar[dict | None] = ContextVar(
    "cassette_call_context", default=None
)


class CassetteError(RuntimeError):
    pass


class RecordedProviderError(RuntimeError):
    """A captured failed attempt stays a failed attempt, without private error text."""


class RecordedProviderTimeout(TimeoutError):
    pass


class RecordedProviderValidationError(ValueError):
    pass


def failure_data(exc):
    from app.agent.execution import ExecutionFailure

    if isinstance(exc, ExecutionFailure):
        return {
            "type": "ExecutionFailure",
            "kind": "execution",
            "code": exc.code,
            "status_code": None,
        }
    return {
        "type": type(exc).__name__,
        "kind": "timeout"
        if isinstance(exc, TimeoutError) or type(exc).__name__ == "APITimeoutError"
        else "validation"
        if isinstance(exc, ValueError) or type(exc).__name__ == "ValidationError"
        else "provider",
        "status_code": getattr(exc, "status_code", None)
        if isinstance(getattr(exc, "status_code", None), int)
        else None,
    }


def replay_failure(data):
    if data["kind"] == "execution":
        from app.agent.execution import ExecutionFailure

        return ExecutionFailure(data["code"])
    cls = {
        "timeout": RecordedProviderTimeout,
        "validation": RecordedProviderValidationError,
        "provider": RecordedProviderError,
    }[data["kind"]]
    error = cls(data["type"])
    error.status_code = data["status_code"]
    return error


def plain(value):
    """Only supported JSON, SDK model_dump and response namespace shapes."""
    if value is None or type(value) in (str, int, float, bool):
        # Also rejects NaN/Infinity below at the serialization boundary.
        return value
    if isinstance(value, dict):
        if any(not isinstance(k, str) for k in value):
            raise TypeError("Recording object keys must be strings")
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if hasattr(value, "model_dump"):
        return plain(value.model_dump(mode="json"))
    if isinstance(value, SimpleNamespace):
        return plain(vars(value))
    # The app's Claude adapter is a documented Responses-shaped object.
    from app.agent.claude_client import _AdapterResponse

    if isinstance(value, _AdapterResponse):
        return plain(vars(value))
    raise TypeError("Unsupported recording value: " + type(value).__name__)


def encoded(value):
    return json.dumps(
        plain(value), sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def sha(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def restored(value):
    if isinstance(value, dict):
        return SimpleNamespace(**{k: restored(v) for k, v in value.items()})
    if isinstance(value, list):
        return [restored(v) for v in value]
    return value


def response_data(response):
    result = plain(response)
    if not isinstance(result, dict):
        raise TypeError("Provider response must be an object")
    # SDK output_text is a computed property, absent from model_dump().
    if hasattr(response, "output_text"):
        result["output_text"] = plain(response.output_text)
    encoded(result)
    return result


def unsuccessful_attempt(record):
    """Classify terminal metadata without altering the preserved provider response.

    Claude/OpenRouter adapters may omit optional Responses status fields. A
    present non-success status or non-null error/incomplete detail is a failure,
    even when the application accepts output_text or falls back successfully.
    """
    if record.get("error"):
        return "raised_error"
    if record.get("execution", {}).get("bound_overrun"):
        return "execution_bound_overrun"
    response = record.get("response", {})
    if response.get("stop_reason") in ("max_tokens", "refusal", "pause_turn"):
        return "provider_stop:" + response["stop_reason"]
    if response.get("finish_reason") not in (None, "stop", "tool_calls"):
        return "provider_finish:" + str(response["finish_reason"])
    if response.get("status") not in (None, "completed"):
        return "response_status:" + str(response["status"])
    if response.get("error") is not None:
        return "response_error"
    if response.get("incomplete_details") is not None:
        return "response_incomplete"
    return None


def settings_binding(settings):
    """Bind settings without secrets or volatile materialized attachment paths."""
    from dataclasses import asdict
    from datetime import date

    values = asdict(settings)
    for key in list(values):
        if isinstance(values[key], date):
            values[key] = values[key].isoformat()
        if any(word in key for word in ("api_key", "password", "secret", "token")):
            # max_output_tokens affects generation and must remain bound.
            if key.endswith("api_key") or any(w in key for w in ("password", "secret")):
                values[key] = "<credential>"
        if key in ("research_availability_path", "agent_teammate_study_path"):
            values[key] = bool(values[key])
    return sha(values)


def _require(condition):
    if not condition:
        raise ValueError("Invalid recording invariant")


def validate_record(record, binding):
    try:
        _require(
            set(record)
            == {
                "key",
                "match",
                "request",
                "execution",
                "chunks",
                "response" if "response" in record else "error",
            }
        )
        key = record["key"]
        _require(isinstance(key, list) and len(key) == 4)
        _require(key[0] in binding["cases"] and type(key[1]) is int and (key[1] >= 0))
        _require(key[2] in ("json", "sse") and type(key[3]) is int and (key[3] >= 0))
        _require(
            isinstance(record["chunks"], list)
            and all((isinstance(c, str) for c in record["chunks"]))
        )
        _require(isinstance(record["execution"], dict))
        _require(("response" in record) != ("error" in record))
        request, match = record["request"], record["match"]
        _require(isinstance(request, dict) and isinstance(match, dict))
        _require(
            set(match) == {"provider", "model", "request_sha256", "settings_sha256"}
        )
        _require(sha(request) == match["request_sha256"])
        _require(match["provider"] == binding["provider"])
        _require(request["model"] == match["model"] == binding["model"])
        _require(isinstance(match["settings_sha256"], str))
        if "response" in record:
            _require(isinstance(record["response"], dict))
        else:
            error = record["error"]
            _require(isinstance(error, dict) and isinstance(error["type"], str))
            _require(
                error["kind"] in ("timeout", "validation", "provider", "execution")
            )
            _require(error["status_code"] is None or type(error["status_code"]) is int)
            if error["kind"] == "execution":
                _require(isinstance(error["code"], str) and error["code"])
    except (AssertionError, KeyError, TypeError, ValueError) as exc:
        raise CassetteError("invalid_record_shape_or_binding") from exc


def verify_legacy(directory):
    from scripts.ask_eval.legacy_recording import RAW_SHA256

    try:
        _require(
            all(
                (
                    hashlib.sha256((directory / name).read_bytes()).hexdigest() == value
                    for name, value in RAW_SHA256.items()
                )
            )
        )
    except (ValueError, OSError) as exc:
        raise CassetteError("untrusted_legacy_recording") from exc


class CassetteStore:
    def __init__(self, directory, mode, binding):
        self.directory = Path(directory)
        self.mode = mode
        self.binding = binding
        self.lock = Lock()
        self.errors = []
        self.counters = {}
        self.consumed = set()
        self.records = {}
        if mode == "record":
            private_directory(self.directory)
            self.manifest = {"schema_version": VERSION, **binding}
            self._write("manifest.json", self.manifest)
        else:
            self.manifest = json.loads((self.directory / "manifest.json").read_text())
            version = self.manifest.get("schema_version")
            if version not in SUPPORTED_VERSIONS:
                raise CassetteError("unsupported_schema")
            if version == 1:
                verify_legacy(self.directory)
            # Adapter/code revisions are provenance, not replay compatibility gates.
            for key, value in binding.items():
                if (
                    key not in ("adapter_revision", "corpus_sha256")
                    and self.manifest.get(key) != value
                ):
                    raise CassetteError("manifest_mismatch:" + key)
            terminal = json.loads((self.directory / "terminal.json").read_text())
            if version == VERSION and terminal.get("manifest_sha256") != sha(
                self.manifest
            ):
                raise CassetteError("manifest_integrity_mismatch")
            for name, expected in terminal["files"].items():
                path = self.directory / name
                if path.name != name or not name.startswith("call-"):
                    raise CassetteError("invalid_call_path")
                record = json.loads(path.read_text())
                if sha(record) != expected:
                    raise CassetteError("corrupt_call:" + name)
                validate_record(record, self.manifest)
                key = tuple(record["key"])
                if key in self.records:
                    raise CassetteError("duplicate_call")
                self.records[key] = record
            if set(terminal["files"]) != {
                p.name for p in self.directory.glob("call-*.json")
            }:
                raise CassetteError("unexpected_call_files")
            if terminal.get("errors"):
                raise CassetteError("recording_harness_errors")
            if terminal.get("calls") != len(self.records):
                raise CassetteError("recording_call_accounting_mismatch")

    def _write(self, name, value):
        with private_open(self.directory / name) as stream:
            stream.write(encoded(value) + "\n")

    def fail(self, reason):
        with self.lock:
            self.errors.append(reason)
        raise CassetteError(reason)

    def claim(self, provider, request, execution):
        context = call_context.get()
        if not context:
            self.fail("missing_request_context")
        with self.lock:
            prefix = (context["case"], context["turn"], context["transport"])
            ordinal = self.counters.get(prefix, 0)
            self.counters[prefix] = ordinal + 1
        key = (*prefix, ordinal)
        match = {
            "provider": provider,
            "model": request.get("model"),
            "request_sha256": sha(request),
            "settings_sha256": context["settings_sha256"],
        }
        if (
            provider != self.binding["provider"]
            or request.get("model") != self.binding["model"]
        ):
            self.fail("provider_model_mismatch")
        if self.mode == "replay":
            record = self.records.get(key)
            if record is None:
                self.fail("missing_call:" + str(key))
            for field, value in match.items():
                if record["match"].get(field) != value:
                    self.fail("call_mismatch:" + field + ":" + str(key))
            with self.lock:
                self.consumed.add(key)
            return key, record
        return key, {
            "key": list(key),
            "match": match,
            "request": request,
            "execution": execution,
        }

    def save(self, key, record):
        with self.lock:
            validate_record(record, self.binding)
            self._write(f"call-{len(self.records):04d}.json", record)
            self.records[key] = record

    def terminal(self):
        with self.lock:
            errors = list(self.errors)
            if self.mode == "replay":
                errors.extend(
                    "unused_call:" + str(k) for k in self.records.keys() - self.consumed
                )
            if self.mode == "record" and sum(self.counters.values()) != len(
                self.records
            ):
                errors.append("unsaved_claimed_calls")
            files = {
                p.name: sha(json.loads(p.read_text()))
                for p in sorted(self.directory.glob("call-*.json"))
            }
            result = {
                "errors": errors,
                "manifest_sha256": sha(self.manifest),
                "files": files,
                "calls": sum(self.counters.values()),
                "consumed": len(self.consumed),
                "failed_attempts": sum(
                    unsuccessful_attempt(r) is not None for r in self.records.values()
                ),
                "unsuccessful_attempts": [
                    {"key": list(key), "reason": reason}
                    for key, record in self.records.items()
                    if (reason := unsuccessful_attempt(record)) is not None
                ],
            }
            if (
                self.mode == "record"
                and not (self.directory / "terminal.json").exists()
            ):
                self._write("terminal.json", result)
            return {
                **result,
                "cassette_sha256": sha({"manifest": self.manifest, "files": files}),
            }


class CassetteClient:
    def __init__(self, store, provider, inner=None, execution=None):
        self.store = store
        self.provider = provider
        self.inner = inner
        self.execution = execution or {}
        self.responses = SimpleNamespace(create=self.create)

    def with_options(self, **kwargs):
        inner = self.inner.with_options(**kwargs) if self.inner is not None else None
        return CassetteClient(
            self.store, self.provider, inner, {**self.execution, **kwargs}
        )

    def create(self, **kwargs):
        callback = kwargs.get("on_output_text_delta")
        try:
            request = plain(
                {
                    k: v
                    for k, v in kwargs.items()
                    if k not in ("on_output_text_delta", "timeout", "max_retries")
                }
            )
            encoded(request)
            execution = plain(
                {
                    **self.execution,
                    **{k: kwargs[k] for k in ("timeout", "max_retries") if k in kwargs},
                }
            )
            key, record = self.store.claim(self.provider, request, execution)
        except CassetteError:
            raise
        except Exception as exc:
            self.store.fail("request_serialization:" + type(exc).__name__)
        if self.store.mode == "replay":
            for chunk in record["chunks"]:
                if callback:
                    callback(chunk)
            if record.get("error"):
                raise replay_failure(record["error"])
            return restored(record["response"])
        chunks = []
        if callback:

            def capture(chunk):
                chunks.append(plain(chunk))
                callback(chunk)

            kwargs["on_output_text_delta"] = capture
        record["chunks"] = chunks
        from app.agent.execution import CURRENT, ExecutionFailure

        policy = CURRENT.get()
        if policy is not None:
            record["execution"]["remaining_seconds"] = max(
                0, policy.deadline - monotonic()
            )
        started = monotonic()

        def finish_timing():
            elapsed = monotonic() - started
            record["execution"]["latency_ms"] = elapsed * 1000
            bounds = [
                record["execution"].get(k) for k in ("timeout", "remaining_seconds")
            ]
            bounds = [b for b in bounds if isinstance(b, (int, float))]
            record["execution"]["bound_overrun"] = bool(
                bounds and elapsed >= min(bounds)
            )

        try:
            result = self.inner.responses.create(**kwargs)
        except Exception as exc:
            finish_timing()
            record["error"] = failure_data(exc)
            self.store.save(key, record)
            from scripts.ask_eval.budget import BudgetExceeded

            if isinstance(exc, ExecutionFailure):
                self.store.errors.append("execution_failure_during_capture:" + exc.code)
            if isinstance(exc, BudgetExceeded):
                self.store.fail("model_call_budget_exhausted")
            raise
        finish_timing()
        try:
            record["response"] = response_data(result)
            self.store.save(key, record)
        except Exception as exc:
            self.store.fail("response_serialization:" + type(exc).__name__)
        return result


def install_network_guard(on_failure):
    """Replay server cannot initiate any network connection, even to a local proxy.

    The independent runner connects inbound to the loopback server; bind/listen/
    accept do not require an outbound exception in this server process.
    """

    def audit(event, args):
        if event in (
            "socket.connect",
            "socket.getaddrinfo",
            "socket.gethostbyname",
            "socket.gethostbyaddr",
            "subprocess.Popen",
            "os.system",
            "os.exec",
            "os.posix_spawn",
            "socket.sendto",
            "socket.sendmsg",
        ):
            on_failure("replay_network_escape")

    sys.addaudithook(audit)
