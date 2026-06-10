from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rate_limit import AgentRateLimiter, InMemoryRateLimitStore


def test_in_memory_store_prunes_expired_keys() -> None:
    store = InMemoryRateLimitStore()
    store.increment("agent:minute:198.51.100.10:100", 60)
    assert "agent:minute:198.51.100.10:100" in store._values

    # Force the entry's expiry and the prune window into the past, then
    # touch a different key: the expired entry must be swept, not kept.
    store._values["agent:minute:198.51.100.10:100"] = (1, 0.0)
    store._next_prune_at = 0.0
    store.increment("agent:minute:198.51.100.11:200", 60)

    assert "agent:minute:198.51.100.10:100" not in store._values
    assert "agent:minute:198.51.100.11:200" in store._values


def test_in_memory_store_counts_within_window() -> None:
    store = InMemoryRateLimitStore()
    assert store.increment("key", 60) == 1
    assert store.increment("key", 60) == 2


def test_agent_rate_limiter_minute_scope_blocks_over_limit() -> None:
    limiter = AgentRateLimiter(InMemoryRateLimitStore())

    first = limiter.check(key="203.0.113.5", per_minute=1, per_day=0)
    second = limiter.check(key="203.0.113.5", per_minute=1, per_day=0)

    assert first.allowed
    assert not second.allowed
    assert second.scope == "minute"
