"""Two-level session cache: L1 in-process LRU, L2 Redis.

Read path:  check LRU cache first; on miss, fetch from Redis and populate L1.
Write path: write-through — update L1 immediately, then persist to Redis.

Sizing rationale: 10,000 entries × ~250 bytes each ≈ 2.5 MB resident memory,
which eliminates one Redis round-trip per transaction for any user seen in the
recent working set — the dominant cost in the geo-velocity detection hot path.
"""

from datetime import datetime
from typing import Any, Optional

from cachetools import LRUCache

from common.metrics import observe_redis_latency, session_cache_requests_total

_SESSION_MAXSIZE = 10_000

# Module-level singleton — shared across all calls within this process.
_cache: LRUCache = LRUCache(maxsize=_SESSION_MAXSIZE)


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
    """Write-through: update L1 immediately, then persist to Redis."""
    entry = {
        "last_latitude": latitude,
        "last_longitude": longitude,
        "last_timestamp": timestamp.isoformat(),
    }
    _cache[user_id] = entry

    key = f"session:{user_id}"
    with observe_redis_latency("session_set"):
        redis_client.hset(
            key,
            mapping={
                "last_latitude": str(latitude),
                "last_longitude": str(longitude),
                "last_timestamp": timestamp.isoformat(),
            },
        )
        redis_client.expire(key, ttl_seconds)
