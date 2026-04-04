"""Containment actions: apply enforcement rules in Redis based on detection type."""

import logging
from typing import Optional

from common.metrics import observe_redis_latency
from common.models import DetectionEvent
from common.redis_client import create_redis_client
from configs.redis_config import RedisConfig

logger = logging.getLogger(__name__)

QUARANTINE_KEY_PREFIX = "enforce:quarantine:user:"
IP_BLOCK_KEY_PREFIX = "block:ip:"


def _sanitize_ip_for_key(ip: str) -> str:
    return ip.replace(":", "-")


def apply_containment(
    detection: DetectionEvent,
    redis_client,
    redis_config: Optional[RedisConfig] = None,
) -> None:
    """
    Apply containment action based on detection_type.
    MVP: replay, geo_velocity_anomaly, and fraud_burst share quarantine (enforce:quarantine:user:{user_id}).
    credential_stuffing: quarantine user and set block:ip:{ip} (TTL 1 hour by default).
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
    elif detection.detection_type == "credential_stuffing":
        qkey = f"{QUARANTINE_KEY_PREFIX}{detection.user_id}"
        with observe_redis_latency("setex_quarantine"):
            redis_client.setex(qkey, config.quarantine_ttl_seconds, "1")
        logger.info(
            "Quarantine set: user_id=%s key=%s ttl=%ds (credential_stuffing)",
            detection.user_id,
            qkey,
            config.quarantine_ttl_seconds,
        )
        if detection.ip_address:
            ip_key = f"{IP_BLOCK_KEY_PREFIX}{_sanitize_ip_for_key(detection.ip_address)}"
            with observe_redis_latency("setex_ip_block"):
                redis_client.setex(ip_key, config.ip_block_ttl_seconds, "1")
            logger.info(
                "IP block set: ip=%s key=%s ttl=%ds",
                detection.ip_address,
                ip_key,
                config.ip_block_ttl_seconds,
            )
        else:
            logger.warning("credential_stuffing detection missing ip_address; IP block skipped")
    else:
        logger.warning("Unknown detection_type=%s, no containment applied", detection.detection_type)
