"""Bound publication work without borrowing legacy request worker capacity."""

import asyncio

from anyio import CancelScope, CapacityLimiter, to_thread


async def _complete_before_cancelling(awaitable):
    operation = asyncio.create_task(awaitable)
    try:
        return await asyncio.shield(operation)
    except asyncio.CancelledError:
        with CancelScope(shield=True):
            while not operation.done():
                try:
                    await asyncio.shield(operation)
                except asyncio.CancelledError:
                    # Repeated native cancellations must not abandon work.
                    continue
                except Exception:
                    break
            # Consume possible errors; caller cancellation owns this outcome.
            if not operation.cancelled():
                operation.exception()
        raise


class PublicationWorkers:
    def __init__(self):
        # Admission covers the whole ASGI request, including body/stream waits.
        # Sync endpoints and controls use a separate worker budget. Queued
        # requests wait asynchronously without exhausting legacy worker tokens.
        self.requests = CapacityLimiter(8)
        self.workers = CapacityLimiter(8)

    async def serve(self, application, *args):
        async with self.requests:
            # Async receive/send/stream waits remain cancellable. Every sync
            # production operation, including endpoints, uses run() below.
            return await application(*args)

    async def run(self, function, *args):
        # Control reads and cold materialization use their own token budget.
        # AnyIO shields CancelScope cancellation, but native Task.cancel() can
        # still interrupt run_sync and free its token while its thread continues.
        # Own the offload in a shielded task and retain admission until it ends.
        return await _complete_before_cancelling(
            to_thread.run_sync(function, *args, limiter=self.workers)
        )
