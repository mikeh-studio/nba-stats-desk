"""Process-wide admission and bounded, nonblocking SSE delivery.

Delivery can detach without cancelling successful computation. The terminal
answer is kept separately from incremental events and is never truncated.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from threading import Lock
from typing import Callable


class CapacityExceeded(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class Admission:
    def __init__(self) -> None:
        self._lock = Lock()
        self._active: set[tuple[str, str]] = set()

    def acquire(self, season: str, conversation: str, limit: int) -> Lease:
        if type(limit) is not int or limit <= 0:
            raise ValueError("Admission limit must be a positive integer")
        key = (season, conversation)
        with self._lock:
            if key in self._active:
                raise CapacityExceeded("conversation_busy")
            if len(self._active) >= limit:
                raise CapacityExceeded("capacity_exceeded")
            self._active.add(key)
            return Lease(self, key, len(self._active), limit)


@dataclass
class Lease:
    admission: Admission
    key: tuple[str, str]
    active: int
    limit: int
    released: bool = False
    worker_finished: bool = False
    delivery_finished: bool = False

    def complete(self, *, worker: bool = False, delivery: bool = False) -> None:
        with self.admission._lock:
            self.worker_finished |= worker
            self.delivery_finished |= delivery
            if self.worker_finished and self.delivery_finished and not self.released:
                self.admission._active.remove(self.key)
                self.released = True

    def release(self) -> None:
        with self.admission._lock:
            if not self.released:
                self.admission._active.remove(self.key)
                self.released = True


class Delivery:
    def __init__(
        self,
        *,
        max_bytes: int,
        max_events: int,
        on_detach: Callable[[], None] | None = None,
    ) -> None:
        self._loop = asyncio.get_running_loop()
        self._ready = asyncio.Event()
        self._lock = Lock()
        self._events: deque[bytes] = deque()
        self._bytes = 0
        self.max_bytes = max_bytes
        self.max_events = max_events
        self.peak_bytes = 0
        self.peak_events = 0
        self.suppressed_events = 0
        self.overflowed = False
        self.detached = False
        self._notified = False
        self._done = False
        self._terminal: tuple[bytes, int] | None = None
        self._on_detach = on_detach

    def _notify_locked(self) -> None:
        # At most one pending event-loop notification, even for a fast producer.
        if not self._notified:
            self._notified = True
            try:
                self._loop.call_soon_threadsafe(self._signal)
            except RuntimeError:
                # The request's loop may have gone away while work finishes.
                self.detached = True
                self._events.clear()
                self._bytes = 0
                self._terminal = None

    def _signal(self) -> None:
        with self._lock:
            self._notified = False
            self._ready.set()

    def put(self, event: bytes) -> None:
        with self._lock:
            if self.detached or self.overflowed or self._done:
                self.suppressed_events += 1
                return
            if (
                len(self._events) >= self.max_events
                or self._bytes + len(event) > self.max_bytes
            ):
                # Keep accepted deltas in order; the complete final replaces the
                # interim presentation. Never block governed work on delivery.
                self.overflowed = True
                self.suppressed_events += 1
                return
            self._events.append(event)
            self._bytes += len(event)
            self.peak_bytes = max(self.peak_bytes, self._bytes)
            self.peak_events = max(self.peak_events, len(self._events))
            self._notify_locked()

    def finish(self, event: bytes, status: int = 200) -> None:
        with self._lock:
            if self._done:
                raise RuntimeError("Delivery already finished")
            self._done = True
            if not self.detached:
                self._terminal = (event, status)
                self._notify_locked()
        if self.detached and self._on_detach:
            self._on_detach()

    async def next(self) -> tuple[bytes, int] | None:
        while True:
            with self._lock:
                if self._events:
                    event = self._events.popleft()
                    self._bytes -= len(event)
                    return event, 200
                if self._terminal is not None:
                    result = self._terminal
                    self._terminal = None
                    return result
                if self._done or self.detached:
                    return None
                self._ready.clear()
            await self._ready.wait()

    def detach(self) -> None:
        with self._lock:
            self.detached = True
            self._events.clear()
            self._bytes = 0
            self._terminal = None
            self._notify_locked()
        if self._on_detach:
            self._on_detach()

    def metrics(self) -> dict[str, int | bool]:
        with self._lock:
            return {
                "stream_peak_bytes": self.peak_bytes,
                "stream_peak_events": self.peak_events,
                "stream_suppressed_events": self.suppressed_events,
                "stream_overflowed": self.overflowed,
                "delivery_detached": self.detached,
            }


admission = Admission()
