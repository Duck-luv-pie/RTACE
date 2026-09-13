"""Geo velocity detection: impossible travel between two transactions from the same user.

The distance/velocity computation and the trusted-baseline rules run inside
lua/tx_check.lua against the ``session:{user_id}`` hash (fields lat, lon, ts):

- no prior session: this transaction becomes the baseline;
- transaction older than the baseline: ignored (never rewinds the baseline);
- velocity above the threshold: anomaly, baseline kept (a flagged location
  must not become trusted or the next purchase from home is flagged too);
- otherwise: baseline advances.

This module keeps a Python Haversine for cross-checking the Lua one in tests
and turns the script's result into a DetectionEvent.
"""

import math
from datetime import datetime, timezone
from typing import Optional, Sequence

from common.models import DetectionEvent, TransactionEvent, new_detection_id
from configs.redis_config import RedisConfig
from detection_engine.scripts import (
    TX_DISTANCE_KM,
    TX_ELAPSED_S,
    TX_GEO_STATUS,
    TX_PREV_LAT,
    TX_PREV_LON,
    TX_VELOCITY_KMH,
)

MIN_TIME_SECONDS = 1.0


def session_key(user_id: str) -> str:
    return f"session:{user_id}"


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km (reference implementation; the hot path uses Lua)."""
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(min(a, 1.0)))


def interpret(tx: TransactionEvent, result: Sequence, config: RedisConfig) -> Optional[DetectionEvent]:
    if result[TX_GEO_STATUS] != "anomaly":
        return None
    return DetectionEvent(
        detection_id=new_detection_id("geo"),
        detection_type="geo_velocity_anomaly",
        severity="high",
        user_id=tx.user_id,
        transaction_id=tx.event_id,
        timestamp=datetime.now(timezone.utc),
        details={
            "distance_km": float(result[TX_DISTANCE_KM]),
            "elapsed_seconds": float(result[TX_ELAPSED_S]),
            "velocity_kmh": float(result[TX_VELOCITY_KMH]),
            "threshold_kmh": config.geo_max_velocity_kmh,
            "from_location": [float(result[TX_PREV_LAT]), float(result[TX_PREV_LON])],
            "to_location": [tx.latitude, tx.longitude],
        },
    )
