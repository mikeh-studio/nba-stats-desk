from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from time import time
from typing import Protocol


class RateLimitStore(Protocol):
    def increment(self, key: str, window_seconds: int) -> int: ...


class InMemoryRateLimitStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._values: dict[str, tuple[int, float]] = {}

    def increment(self, key: str, window_seconds: int) -> int:
        now = time()
        expires_at = now + window_seconds
        with self._lock:
            count, current_expires_at = self._values.get(key, (0, expires_at))
            if current_expires_at <= now:
                count = 0
                current_expires_at = expires_at
            count += 1
            self._values[key] = (count, current_expires_at)
            return count


class RedisRateLimitStore:
    def __init__(self, redis_url: str) -> None:
        try:
            import redis
        except ImportError as exc:  # pragma: no cover - dependency-specific branch
            raise RuntimeError(
                "redis package is required for Redis rate limiting"
            ) from exc
        self._client = redis.Redis.from_url(redis_url)

    def increment(self, key: str, window_seconds: int) -> int:
        pipe = self._client.pipeline()
        pipe.incr(key)
        pipe.expire(key, window_seconds, nx=True)
        count, _ = pipe.execute()
        return int(count)


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    scope: str | None = None
    limit: int | None = None


class AgentRateLimiter:
    def __init__(self, store: RateLimitStore) -> None:
        self.store = store

    def check(
        self,
        *,
        key: str,
        per_minute: int,
        per_day: int,
    ) -> RateLimitDecision:
        if per_minute > 0:
            minute_key = f"agent:minute:{key}:{int(time() // 60)}"
            count = self.store.increment(minute_key, 60)
            if count > per_minute:
                return RateLimitDecision(False, scope="minute", limit=per_minute)
        if per_day > 0:
            day = datetime.now(UTC).strftime("%Y%m%d")
            day_key = f"agent:day:{key}:{day}"
            count = self.store.increment(day_key, 86400)
            if count > per_day:
                return RateLimitDecision(False, scope="day", limit=per_day)
        return RateLimitDecision(True)


_memory_store = InMemoryRateLimitStore()
_redis_limiters: dict[str, AgentRateLimiter] = {}
_redis_lock = Lock()


def get_agent_rate_limiter(redis_url: str | None) -> AgentRateLimiter:
    if not redis_url:
        return AgentRateLimiter(_memory_store)
    with _redis_lock:
        limiter = _redis_limiters.get(redis_url)
        if limiter is None:
            limiter = AgentRateLimiter(RedisRateLimitStore(redis_url))
            _redis_limiters[redis_url] = limiter
        return limiter
