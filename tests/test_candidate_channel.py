"""The candidate authority channel is local, bounded and fail closed."""

import json
import socket
import struct
import threading
from datetime import datetime, timedelta, timezone

import pytest
from app.serving.candidate_channel import CandidateBindingChannel
from app.serving.contract import ServingError, content_digest


def binding():
    return {
        "journal_id": content_digest("journal"),
        "generation": 1,
        "record_id": content_digest("record"),
        "candidate_id": content_digest("candidate"),
        "code_id": content_digest("code"),
        "catalog_id": content_digest("catalog"),
        "status_id": content_digest("status"),
        "status_revision": 1,
        "snapshot_ids": [content_digest("snapshot")],
        "context_ids": [content_digest("context")],
        "selection_id": None,
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    }


def test_socketpair_binding_roundtrip_and_fragmented_frame():
    reader, writer = socket.socketpair()
    expected = binding()

    def serve():
        request = writer.recv(39)
        assert request.startswith(b"CHECK ") and request.endswith(b"\n")
        nonce = request[6:-1].decode()
        data = json.dumps({"nonce": nonce, "binding": expected}).encode()
        frame = struct.pack("!I", len(data)) + data
        for offset in range(0, len(frame), 7):
            writer.sendall(frame[offset : offset + 7])

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        result = CandidateBindingChannel(reader)()
        assert result.generation == 1
        assert result.snapshot_ids == tuple(expected["snapshot_ids"])
        assert result.expires_at.isoformat() == expected["expires_at"]
    finally:
        thread.join(timeout=5)
        reader.close()
        writer.close()


@pytest.mark.parametrize(
    "body", [b'{"generation":1,"generation":2}', b"{" + b"x" * 17000, b"NaN", b"{}"]
)
def test_socketpair_refuses_invalid_duplicate_or_oversized_binding(body):
    reader, writer = socket.socketpair()

    def serve():
        writer.recv(39)
        try:
            writer.sendall(struct.pack("!I", len(body)) + body)
        except BrokenPipeError:
            pass

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        with pytest.raises(ServingError):
            CandidateBindingChannel(reader)()
    finally:
        thread.join(timeout=5)
        reader.close()
        writer.close()


def test_socketpair_timeout_refuses():
    reader, writer = socket.socketpair()
    try:
        with pytest.raises(ServingError, match="candidate_authority_unavailable"):
            CandidateBindingChannel(reader, timeout_seconds=0.05)()
    finally:
        reader.close()
        writer.close()


def test_timed_out_channel_cannot_consume_delayed_response_on_next_check():
    reader, writer = socket.socketpair()
    first_received = threading.Event()
    release = threading.Event()
    outcome = []

    def serve():
        request = writer.recv(39)
        assert request.startswith(b"CHECK ") and request.endswith(b"\n")
        nonce = request[6:-1].decode()
        first_received.set()
        release.wait(2)
        frame = json.dumps({"nonce": nonce, "binding": binding()}).encode()
        try:
            writer.sendall(struct.pack("!I", len(frame)) + frame)
            outcome.append(writer.recv(39))
        except BrokenPipeError:
            outcome.append(b"")

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        channel = CandidateBindingChannel(reader, timeout_seconds=0.05)
        with pytest.raises(ServingError):
            channel()
        assert first_received.is_set()
        release.set()
        thread.join(timeout=2)
        with pytest.raises(ServingError):
            channel()
        assert outcome == [b""]
    finally:
        release.set()
        thread.join(timeout=2)
        reader.close()
        writer.close()


def test_socketpair_rejects_mismatched_nonce_and_is_poisoned():
    reader, writer = socket.socketpair()

    def serve():
        writer.recv(39)
        body = json.dumps({"nonce": "0" * 32, "binding": binding()}).encode()
        writer.sendall(struct.pack("!I", len(body)) + body)

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        channel = CandidateBindingChannel(reader)
        with pytest.raises(ServingError):
            channel()
        with pytest.raises(ServingError):
            channel()
    finally:
        thread.join(timeout=2)
        reader.close()
        writer.close()
