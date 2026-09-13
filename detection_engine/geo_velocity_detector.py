"""Geo velocity detection: impossible travel between two transactions from the same user."""

import math
from datetime import datetime, timezone
from typing import Optional

from common import session_cache
from common.models import DetectionEvent, TransactionEvent, new_detection_id
from configs.redis_config import RedisConfig

# Two transactions closer together than this are treated as one second apart so
# that a large distance in a tiny interval is scored as *very* fast, not skipped.
MIN_TIME_SECONDS = 1.0


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


def _parse_ts(raw: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def check_geo_velocity(
    tx: TransactionEvent,
    redis_client,
    redis_config: Optional[RedisConfig] = None,
) -> Optional[DetectionEvent]:
    """Flag impossible travel relative to the user's last *trusted* location.

    Rules:
    - No prior session: record this transaction as the baseline, no detection.
    - Transaction older than the baseline (out-of-order delivery or clock skew):
      ignore it entirely. Rewinding the baseline to a stale point would make the
      next legitimate transaction look like a jump.
    - Velocity above ``geo_max_velocity_kmh``: emit ``geo_velocity_anomaly`` and
      keep the existing baseline. The anomalous location must not become the new
      trusted position, otherwise the user's next transaction from home is
      flagged as well (ping-pong). A genuine relocation is accepted naturally
      once enough time has passed that the implied speed is plausible.
    - Otherwise: advance the baseline to this transaction.
    """
    config = redis_config or RedisConfig.from_env()
    ttl_seconds = config.session_ttl_days * 24 * 3600
    tx_ts = tx.timestamp if tx.timestamp.tzinfo else tx.timestamp.replace(tzinfo=timezone.utc)

    session = session_cache.get_session(redis_client, tx.user_id)
    last_ts = _parse_ts(session["last_timestamp"]) if session else None

    if session is None or last_ts is None:
        session_cache.set_session(
            redis_client, tx.user_id, tx.latitude, tx.longitude, tx_ts, ttl_seconds
        )
        return None

    delta_seconds = (tx_ts - last_ts).total_seconds()
    if delta_seconds < 0:
        return None

    distance_km = haversine_km(
        session["last_latitude"], session["last_longitude"], tx.latitude, tx.longitude
    )
    time_hours = max(delta_seconds, MIN_TIME_SECONDS) / 3600.0
    velocity_kmh = distance_km / time_hours

    if velocity_kmh > config.geo_max_velocity_kmh:
        return DetectionEvent(
            detection_id=new_detection_id("geo"),
            detection_type="geo_velocity_anomaly",
            severity="high",
            user_id=tx.user_id,
            transaction_id=tx.event_id,
            timestamp=datetime.now(timezone.utc),
            details={
                "distance_km": round(distance_km, 1),
                "elapsed_seconds": round(delta_seconds, 1),
                "velocity_kmh": round(velocity_kmh, 1),
                "threshold_kmh": config.geo_max_velocity_kmh,
                "from_location": [session["last_latitude"], session["last_longitude"]],
                "to_location": [tx.latitude, tx.longitude],
            },
        )

    session_cache.set_session(
        redis_client, tx.user_id, tx.latitude, tx.longitude, tx_ts, ttl_seconds
    )
    return None
