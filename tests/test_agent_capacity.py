"""Offline checks of shared admission and delivery behavior under contention."""

import asyncio
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier, Thread

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.capacity import Admission, CapacityExceeded, Delivery


def test_admission_caps_simultaneous_work_and_recovers_after_workers_release():
    admission = Admission()
    entrants = 12
    cap = 3
    start = Barrier(entrants)
    acquired = Barrier(entrants)

    def worker(index):
        start.wait(timeout=10)
        lease = None
        code = None
        try:
            lease = admission.acquire("2024-25", f"conversation-{index}", cap)
        except CapacityExceeded as exc:
            code = exc.code
        acquired.wait(timeout=10)
        if lease:
            lease.release()
            lease.release()
        return lease is not None, code

    with ThreadPoolExecutor(max_workers=entrants) as pool:
        results = list(pool.map(worker, range(entrants)))
    assert sum(success for success, _ in results) == cap
    assert [code for success, code in results if not success] == [
        "capacity_exceeded"
    ] * (entrants - cap)
    leases = [admission.acquire("2024-25", str(i), cap) for i in range(cap)]
    assert [lease.active for lease in leases] == [1, 2, 3]
    for lease in leases:
        lease.release()


def test_conversation_lock_is_season_scoped_and_capacity_is_process_shared():
    admission = Admission()
    first = admission.acquire("2024-25", "same-conversation", 2)
    with pytest.raises(CapacityExceeded, match="conversation_busy"):
        admission.acquire("2024-25", "same-conversation", 2)
    next_season = admission.acquire("2025-26", "same-conversation", 2)
    with pytest.raises(CapacityExceeded, match="capacity_exceeded"):
        admission.acquire("2026-27", "other-conversation", 2)
    first.release()
    retry = admission.acquire("2024-25", "same-conversation", 2)
    next_season.release()
    retry.release()


@pytest.mark.parametrize("max_bytes,max_events", [(6, 100), (100, 2)])
def test_delivery_overflow_preserves_prefix_and_complete_terminal(
    max_bytes, max_events
):
    async def scenario():
        delivery = Delivery(max_bytes=max_bytes, max_events=max_events)
        for event in [b"one", b"two", b"three", b"four"]:
            delivery.put(event)
        final = b"complete answer considerably larger than incremental buffer"
        delivery.finish(final)
        assert await delivery.next() == (b"one", 200)
        assert await delivery.next() == (b"two", 200)
        assert await delivery.next() == (final, 200)
        assert await delivery.next() is None
        metrics = delivery.metrics()
        assert metrics["stream_peak_bytes"] <= max_bytes
        assert metrics["stream_peak_events"] <= max_events
        assert metrics["stream_suppressed_events"] == 2
        assert metrics["stream_overflowed"] is True
        assert metrics["delivery_detached"] is False

    asyncio.run(scenario())


def test_single_oversized_increment_does_not_block_failure_terminal():
    async def scenario():
        delivery = Delivery(max_bytes=1, max_events=1)
        delivery.put(b"oversized")
        delivery.finish(b"safe failure", 502)
        assert await asyncio.wait_for(delivery.next(), timeout=1) == (
            b"safe failure",
            502,
        )
        assert await delivery.next() is None
        assert delivery.metrics()["stream_peak_bytes"] == 0

    asyncio.run(scenario())


def test_detach_wakes_consumer_and_discards_delivery_without_blocking_worker():
    async def scenario():
        delivery = Delivery(max_bytes=6, max_events=2)
        waiting = asyncio.create_task(delivery.next())
        await asyncio.sleep(0)
        worker = Thread(target=delivery.detach)
        worker.start()
        worker.join(timeout=1)
        assert not worker.is_alive()
        assert await asyncio.wait_for(waiting, timeout=1) is None
        delivery.put(b"future")
        delivery.finish(b"success computed after client left")
        assert await delivery.next() is None
        assert delivery.metrics()["delivery_detached"] is True
        assert delivery.metrics()["stream_suppressed_events"] == 1

    asyncio.run(scenario())


def test_fast_producer_coalesces_loop_notifications(monkeypatch):
    async def scenario():
        loop = asyncio.get_running_loop()
        original = loop.call_soon_threadsafe
        count = 0

        def notify(*args, **kwargs):
            nonlocal count
            count += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(loop, "call_soon_threadsafe", notify)
        delivery = Delivery(max_bytes=1000, max_events=1000)
        producer = Thread(target=lambda: [delivery.put(b"x") for _ in range(1000)])
        producer.start()
        producer.join(timeout=2)
        assert not producer.is_alive()
        delivery.finish(b"final")
        assert count == 1
        for _ in range(1000):
            assert await delivery.next() == (b"x", 200)
        assert await delivery.next() == (b"final", 200)
        assert await delivery.next() is None

    asyncio.run(scenario())


def test_delivery_consumer_resumes_after_draining_previous_notification():
    async def scenario():
        delivery = Delivery(max_bytes=8, max_events=2)
        delivery.put(b"first")
        assert await delivery.next() == (b"first", 200)
        waiting = asyncio.create_task(delivery.next())
        await asyncio.sleep(0)
        producer = Thread(target=lambda: delivery.put(b"next"))
        producer.start()
        producer.join(timeout=1)
        assert await asyncio.wait_for(waiting, timeout=1) == (b"next", 200)
        delivery.finish(b"final")
        assert await delivery.next() == (b"final", 200)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "field,value",
    [
        ("agent_max_concurrent_requests", 0),
        ("agent_max_concurrent_requests", -1),
        ("agent_max_concurrent_requests", True),
        ("agent_stream_buffer_bytes", 0),
        ("agent_stream_buffer_bytes", -1),
        ("agent_stream_buffer_bytes", 1.5),
        ("agent_stream_buffer_events", 0),
        ("agent_stream_buffer_events", None),
    ],
)
def test_invalid_capacity_configuration_fails_at_startup(field, value):
    from tests.test_agent_service import _settings

    with pytest.raises(ValueError, match=f"{field} must be a positive integer"):
        replace(_settings(), **{field: value})


def test_default_capacity_inherits_runtime_and_explicit_override_remains_available():
    from tests.test_agent_service import _settings

    assert _settings().agent_max_concurrent_requests is None
    assert (
        replace(
            _settings(), agent_max_concurrent_requests=7
        ).agent_max_concurrent_requests
        == 7
    )


@pytest.mark.parametrize("first", ["worker", "delivery"])
def test_lease_retains_capacity_until_both_owners_finish(first):
    admission = Admission()
    lease = admission.acquire("2024-25", "original", 1)
    lease.complete(**{first: True})
    lease.complete(**{first: True})
    assert not lease.released
    with pytest.raises(CapacityExceeded, match="capacity_exceeded"):
        admission.acquire("2024-25", "next", 1)
    with pytest.raises(CapacityExceeded, match="conversation_busy"):
        admission.acquire("2024-25", "original", 1)
    lease.complete(**{"delivery" if first == "worker" else "worker": True})
    assert lease.released
    lease.complete(worker=True, delivery=True)
    retry = admission.acquire("2024-25", "original", 1)
    retry.release()


def test_simultaneous_owner_completions_release_capacity_once():
    admission = Admission()
    lease = admission.acquire("2024-25", "original", 1)
    barrier = Barrier(2)

    def finish(owner):
        barrier.wait(timeout=10)
        lease.complete(**{owner: True})

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(finish, ["worker", "delivery"]))
    assert lease.released
    retry = admission.acquire("2024-25", "original", 1)
    retry.release()


def test_terminal_retention_and_response_send_keep_admission_slot():
    async def scenario():
        admission = Admission()
        lease = admission.acquire("2024-25", "original", 1)
        delivery = Delivery(
            max_bytes=1,
            max_events=1,
            on_detach=lambda: lease.complete(delivery=True),
        )
        delivery.finish(b"complete terminal answer")
        lease.complete(worker=True)
        with pytest.raises(CapacityExceeded, match="capacity_exceeded"):
            admission.acquire("2024-25", "next", 1)
        assert await delivery.next() == (b"complete terminal answer", 200)
        # Reading the result is distinct from completing ASGI send.
        assert await delivery.next() is None
        with pytest.raises(CapacityExceeded, match="capacity_exceeded"):
            admission.acquire("2024-25", "next", 1)
        delivery.detach()
        delivery.detach()
        retry = admission.acquire("2024-25", "next", 1)
        retry.release()

    asyncio.run(scenario())


def test_progress_after_finish_is_suppressed_before_and_after_terminal_read():
    async def scenario():
        delivery = Delivery(max_bytes=100, max_events=10)
        delivery.finish(b"final")
        delivery.put(b"late progress")
        assert await delivery.next() == (b"final", 200)
        delivery.put(b"even later progress")
        assert await delivery.next() is None
        assert delivery.metrics()["stream_suppressed_events"] == 2
        assert delivery.metrics()["stream_peak_bytes"] == 0
        with pytest.raises(RuntimeError, match="Delivery already finished"):
            delivery.finish(b"duplicate final")

    asyncio.run(scenario())


def test_closed_event_loop_drops_terminal_and_completes_delivery_ownership():
    admission = Admission()
    lease = admission.acquire("2024-25", "original", 1)

    async def create_delivery():
        return Delivery(
            max_bytes=1,
            max_events=1,
            on_detach=lambda: lease.complete(delivery=True),
        )

    delivery = asyncio.run(create_delivery())
    lease.complete(worker=True)
    delivery.finish(b"finished after request loop closed")
    assert delivery.metrics()["delivery_detached"] is True
    assert lease.released
    retry = admission.acquire("2024-25", "original", 1)
    retry.release()
