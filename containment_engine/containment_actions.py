"""Containment actions: apply enforcement rules in Redis based on detection type."""

import logging
from typing import Optional

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
    For replay_attack: set enforce:quarantine:user:{user_id} with TTL (e.g. 1 hour).
    """
    config = redis_config or RedisConfig.from_env()

    if detection.detection_type == "replay_attack":
        key = f"{QUARANTINE_KEY_PREFIX}{detection.user_id}"
        redis_client.setex(key, config.quarantine_ttl_seconds, "1")
        logger.info(
            "Quarantine set: user_id=%s key=%s ttl=%ds",
            detection.user_id,
            key,
            config.quarantine_ttl_seconds,
        )
    else:
        logger.warning("Unknown detection_type=%s, no containment applied", detection.detection_type)
