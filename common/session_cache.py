"""Two-level session cache: L1 in-process TTL/LRU cache, L2 Redis.

Read path:  check the in-process cache first; on miss, fetch from Redis and populate L1.
Write path: write-through — update L1 immediately, then persist to Redis.

Why a TTL and not a plain LRU: the L1 copy is private to this process. Kafka
partitions are keyed by user_id, so at any instant only one consumer process
sees a given user, but after a rebalance a partition can move away and come
back, at which point the old process must not serve a location it cached
before the partition left. Bounding L1 entries with a short TTL (and clearing
the cache on partition revocation, see ``clear()``) keeps that staleness window
small. The TTL also keeps L1 from outliving the 7-day Redis session TTL.
"""

from datetime import datetime
from typing import Any, Optional

from cachetools import TTLCache

from common.metrics import observe_redis_latency, session_cache_requests_total

_DEFAULT_MAXSIZE = 10_000
_DEFAULT_TTL_SECONDS = 300

_cache: TTLCache = TTLCache(maxsize=_DEFAULT_MAXSIZE, ttl=_DEFAULT_TTL_SECONDS)


def configure(maxsize: int, ttl_seconds: int) -> None:
    """(Re)create the L1 cache with the given bounds. Call once at process start."""
    global _cache
    _cache = TTLCache(maxsize=maxsize, ttl=ttl_seconds)


def clear() -> None:
    """Drop every L1 entry. Call when this process loses Kafka partitions."""
    _cache.clear()


def get_session(redis_client, user_id: str) -> Optional[dict[str, Any]]:
    """Return session data for user_id, consulting L1 before Redis."""
    entry = _cache.get(user_id)
    if entry is not None:
        session_cache_requests_total.labels(result="hit").inc()
        return entry

    session_cache_requests_total.labels(result="miss").inc()
    key = f"session:{user_id}"
    with observe_redis_latency("session_get"):
        raw = redis_client.hgetall(key)
    if not raw:
        return None
    try:
        parsed = {
            "last_latitude": float(raw["last_latitude"]),
            "last_longitude": float(raw["last_longitude"]),
            "last_timestamp": raw["last_timestamp"],
        }
    except (KeyError, ValueError, TypeError):
        return None

    _cache[user_id] = parsed
    return parsed


def set_session(
    redis_client,
    user_id: str,
    latitude: float,
    longitude: float,
    timestamp: datetime,
    ttl_seconds: int,
) -> None:
    """Write-through: update L1 immediately, then persist to Redis in one round trip."""
    entry = {
        "last_latitude": latitude,
        "last_longitude": longitude,
        "last_timestamp": timestamp.isoformat(),
    }
    _cache[user_id] = entry

    key = f"session:{user_id}"
    with observe_redis_latency("session_set"):
        with redis_client.pipeline(transaction=False) as pipe:
            pipe.hset(
                key,
                mapping={
                    "last_latitude": str(latitude),
                    "last_longitude": str(longitude),
                    "last_timestamp": timestamp.isoformat(),
                },
            )
            pipe.expire(key, ttl_seconds)
            pipe.execute()
