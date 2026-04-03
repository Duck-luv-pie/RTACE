"""Containment actions: apply enforcement rules in Redis based on detection type."""

import logging
from typing import Optional

from common.metrics import observe_redis_latency
from common.models import DetectionEvent
from common.redis_client import create_redis_client
from configs.redis_config import RedisConfig

logger = logging.getLogger(__name__)

QUARANTINE_KEY_PREFIX = "enforce:quarantine:user:"


def apply_containment(
    detection: DetectionEvent,
    redis_client,
    redis_config: Optional[RedisConfig] = None,
) -> None:
    """
    Apply containment action based on detection_type.
    MVP: replay, geo_velocity_anomaly, and fraud_burst share quarantine (enforce:quarantine:user:{user_id}).
    Long term: replay → block transaction + maybe quarantine; geo velocity → step-up auth
    first, quarantine only for very high confidence.
    """
    config = redis_config or RedisConfig.from_env()

    if detection.detection_type in (
        "replay_attack",
        "geo_velocity_anomaly",
        "fraud_burst",
    ):
        key = f"{QUARANTINE_KEY_PREFIX}{detection.user_id}"
        with observe_redis_latency("setex_quarantine"):
            redis_client.setex(key, config.quarantine_ttl_seconds, "1")
        logger.info(
            "Quarantine set: user_id=%s key=%s ttl=%ds (type=%s)",
            detection.user_id,
            key,
            config.quarantine_ttl_seconds,
            detection.detection_type,
        )
    else:
        logger.warning("Unknown detection_type=%s, no containment applied", detection.detection_type)
