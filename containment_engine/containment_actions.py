"""Containment actions: apply enforcement rules in Redis based on detection type."""

import logging
from typing import Optional

from common.metrics import observe_redis_latency
from common.models import DetectionEvent
from configs.redis_config import RedisConfig

logger = logging.getLogger(__name__)

QUARANTINE_KEY_PREFIX = "enforce:quarantine:user:"
IP_BLOCK_KEY_PREFIX = "block:ip:"


def _sanitize_ip_for_key(ip: str) -> str:
    return ip.replace(":", "-")


def quarantine_key(user_id: str) -> str:
    return f"{QUARANTINE_KEY_PREFIX}{user_id}"


def ip_block_key(ip: str) -> str:
    return f"{IP_BLOCK_KEY_PREFIX}{_sanitize_ip_for_key(ip)}"


def apply_containment(
    detection: DetectionEvent,
    redis_client,
    redis_config: Optional[RedisConfig] = None,
) -> list[str]:
    """Apply containment for a detection and return the list of actions taken.

    All writes are SETEX and therefore idempotent: redelivering a detection
    re-applies the same rule with a fresh TTL.
    """
    config = redis_config or RedisConfig.from_env()
    actions: list[str] = []

    if detection.detection_type in ("replay_attack", "geo_velocity_anomaly", "fraud_burst"):
        key = quarantine_key(detection.user_id)
        with observe_redis_latency("setex_quarantine"):
            redis_client.setex(key, config.quarantine_ttl_seconds, detection.detection_id)
        actions.append("quarantine")
        logger.info(
            "Quarantine set: user_id=%s key=%s ttl=%ds (type=%s)",
            detection.user_id, key, config.quarantine_ttl_seconds, detection.detection_type,
        )
    elif detection.detection_type == "credential_stuffing":
        qkey = quarantine_key(detection.user_id)
        with observe_redis_latency("setex_quarantine"):
            redis_client.setex(qkey, config.quarantine_ttl_seconds, detection.detection_id)
        actions.append("quarantine")
        logger.info(
            "Quarantine set: user_id=%s key=%s ttl=%ds (credential_stuffing)",
            detection.user_id, qkey, config.quarantine_ttl_seconds,
        )
        if detection.ip_address:
            ikey = ip_block_key(detection.ip_address)
            with observe_redis_latency("setex_ip_block"):
                redis_client.setex(ikey, config.ip_block_ttl_seconds, detection.detection_id)
            actions.append("ip_block")
            logger.info(
                "IP block set: ip=%s key=%s ttl=%ds",
                detection.ip_address, ikey, config.ip_block_ttl_seconds,
            )
        else:
            logger.warning("credential_stuffing detection missing ip_address; IP block skipped")
    else:
        logger.warning("Unknown detection_type=%s, no containment applied", detection.detection_type)

    return actions
