"""Geo velocity detection: impossible travel between two transactions from the same user."""

import math
from datetime import datetime, timezone
from typing import Any, Optional

from common.metrics import observe_redis_latency
from common.models import TransactionEvent, DetectionEvent
from configs.redis_config import RedisConfig

SESSION_KEY_PREFIX = "session:"
MAX_VELOCITY_KMH = 900.0
MIN_TIME_HOURS = 1.0 / 3600.0  # 1 second, avoid div by zero


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compute great-circle distance in km using the Haversine formula."""
    R = 6371.0  # Earth radius in km
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def _session_key(user_id: str) -> str:
    return f"{SESSION_KEY_PREFIX}{user_id}"


def _get_session(redis_client, user_id: str) -> Optional[dict[str, Any]]:
    """Retrieve last location and timestamp from Redis session key. Returns None if missing."""
    key = _session_key(user_id)
    with observe_redis_latency("session_get"):
        raw = redis_client.hgetall(key)
    if not raw:
        return None
    try:
        return {
            "last_latitude": float(raw["last_latitude"]),
            "last_longitude": float(raw["last_longitude"]),
            "last_timestamp": raw["last_timestamp"],
        }
    except (KeyError, ValueError, TypeError):
        return None


def _set_session(
    redis_client,
    user_id: str,
    latitude: float,
    longitude: float,
    timestamp: datetime,
    ttl_seconds: int,
) -> None:
    """Update Redis session with current transaction location and timestamp."""
    key = _session_key(user_id)
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


def check_geo_velocity(
    tx: TransactionEvent,
    redis_client,
    redis_config: Optional[RedisConfig] = None,
) -> Optional[DetectionEvent]:
    """
    Check for impossible travel: if velocity from last known location to current
    exceeds MAX_VELOCITY_KMH (900 km/h), emit a geo_velocity_anomaly detection.
    Always updates Redis session state after the check.
    """
    config = redis_config or RedisConfig.from_env()
    ttl_seconds = config.session_ttl_days * 24 * 3600

    session = _get_session(redis_client, tx.user_id)

    if session is None:
        _set_session(
            redis_client, tx.user_id, tx.latitude, tx.longitude, tx.timestamp, ttl_seconds
        )
        return None

    try:
        last_ts = datetime.fromisoformat(
            session["last_timestamp"].replace("Z", "+00:00")
        )
    except (ValueError, TypeError):
        _set_session(
            redis_client, tx.user_id, tx.latitude, tx.longitude, tx.timestamp, ttl_seconds
        )
        return None

    time_delta_seconds = (tx.timestamp - last_ts).total_seconds()
    time_hours = time_delta_seconds / 3600.0

    if time_hours < MIN_TIME_HOURS:
        _set_session(
            redis_client, tx.user_id, tx.latitude, tx.longitude, tx.timestamp, ttl_seconds
        )
        return None

    distance_km = haversine_km(
        session["last_latitude"],
        session["last_longitude"],
        tx.latitude,
        tx.longitude,
    )
    velocity_kmh = distance_km / time_hours

    if velocity_kmh > MAX_VELOCITY_KMH:
        detection = DetectionEvent(
            detection_id=f"det-geo-{tx.event_id}",
            detection_type="geo_velocity_anomaly",
            severity="high",
            user_id=tx.user_id,
            transaction_id=tx.event_id,
            timestamp=datetime.now(timezone.utc),
        )
    else:
        detection = None

    _set_session(
        redis_client, tx.user_id, tx.latitude, tx.longitude, tx.timestamp, ttl_seconds
    )
    return detection
