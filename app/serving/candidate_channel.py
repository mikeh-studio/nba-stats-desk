"""Bounded local socketpair transport for a separately trusted offline checker."""

from __future__ import annotations

import json
import secrets
import socket
import struct
import threading
import time

from app.serving.candidate_service import CandidateBinding
from app.serving.contract import ServingError

MAX_BINDING_BYTES = 16 * 1024


class CandidateBindingChannel:
    """Request one fresh binding per check from an explicitly inherited socketpair."""

    def __init__(self, connection: socket.socket, *, timeout_seconds: float = 2.0):
        if (
            type(connection) is not socket.socket
            or connection.family != socket.AF_UNIX
            or connection.type & 0xF != socket.SOCK_STREAM
            or type(timeout_seconds) not in (int, float)
            or not 0 < timeout_seconds <= 10
        ):
            raise ServingError("invalid_candidate_channel")
        self._connection = connection
        self._timeout = timeout_seconds
        self._lock = threading.Lock()
        self._failed = False

    def __call__(self):
        if not self._lock.acquire(timeout=self._timeout):
            raise ServingError("candidate_authority_unavailable")
        try:
            if self._failed:
                raise ServingError("candidate_authority_unavailable")
            deadline = time.monotonic() + self._timeout

            def budget():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ServingError("candidate_authority_unavailable")
                self._connection.settimeout(remaining)

            def read_exact(count):
                output = bytearray()
                while len(output) < count:
                    budget()
                    part = self._connection.recv(count - len(output))
                    if not part:
                        raise ServingError("candidate_authority_unavailable")
                    output.extend(part)
                return bytes(output)

            def pairs(items):
                result = {}
                for key, value in items:
                    if key in result:
                        raise ValueError("duplicate key")
                    result[key] = value
                return result

            try:
                budget()
                nonce = secrets.token_hex(16)
                self._connection.sendall(f"CHECK {nonce}\n".encode("ascii"))
                length = struct.unpack("!I", read_exact(4))[0]
                if not 0 < length <= MAX_BINDING_BYTES:
                    raise ServingError("candidate_authority_unavailable")
                value = json.loads(
                    read_exact(length),
                    object_pairs_hook=pairs,
                    parse_constant=lambda _: (_ for _ in ()).throw(
                        ValueError("nonfinite")
                    ),
                )
                if (
                    type(value) is not dict
                    or set(value) != {"nonce", "binding"}
                    or value["nonce"] != nonce
                ):
                    raise ValueError("candidate nonce mismatch")
                return CandidateBinding.from_mapping(value["binding"])
            except (OSError, ValueError, TypeError, UnicodeError, struct.error):
                self._failed = True
                self._connection.close()
                raise ServingError("candidate_authority_unavailable") from None
        finally:
            self._lock.release()
