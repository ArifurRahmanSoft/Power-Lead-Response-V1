import asyncio
from collections import defaultdict, deque
from time import monotonic


class RateLimitExceededError(Exception):
    pass


class SlidingWindowRateLimiter:
    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def enforce(
        self,
        key: str,
        *,
        limit: int,
        window_seconds: int,
    ) -> None:
        now = monotonic()
        cutoff = now - window_seconds
        async with self._lock:
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= limit:
                raise RateLimitExceededError
            events.append(now)


lead_voice_rate_limiter = SlidingWindowRateLimiter()
