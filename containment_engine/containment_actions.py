"""Containment actions: apply enforcement rules in Redis based on detection type.

Policy (see README "Containment policy"):

  replay_attack          -> quarantine user           (hard signal: exact request resent)
  fraud_burst            -> quarantine user           (hard signal: rate far above normal)
  credential_stuffing    -> quarantine user; block the IP only for IP-scope detections.
                            A user-scope detection carries the IP of whichever login
                            tipped the count, which may be the legitimate user; blocking
                            it would be a self-inflicted outage (seen in a live run, where
                            shared office IPs got blocked).
  geo_velocity_anomaly   -> step-up authentication    (soft signal: VPNs, shared accounts,
                                                       coarse geolocation trip it often)
                            escalates to quarantine if another anomaly arrives while a
                            step-up is already pending

All writes are SETEX and therefore idempotent: redelivering a detection
re-applies the same rule with a fresh TTL.
"""

import logging
from typing import Optional

from common.enforcement import ip_block_key, is_step_up_pending, quarantine_key, step_up_key
from common.metrics import observe_redis_latency
from common.models import DetectionEvent
from configs.redis_config import RedisConfig

logger = logging.getLogger(__name__)

HARD_QUARANTINE_TYPES = ("replay_attack", "fraud_burst")


def _quarantine(redis_client, detection: DetectionEvent, config: RedisConfig) -> str:
    key = quarantine_key(detection.user_id)
    with observe_redis_latency("setex_quarantine"):
        redis_client.setex(key, config.quarantine_ttl_seconds, detection.detection_id)
    logger.info(
        "Quarantine set: user_id=%s ttl=%ds (type=%s)",
        detection.user_id, config.quarantine_ttl_seconds, detection.detection_type,
    )
    return "quarantine"


def _step_up(redis_client, detection: DetectionEvent, config: RedisConfig) -> str:
    key = step_up_key(detection.user_id)
    with observe_redis_latency("setex_step_up"):
        redis_client.setex(key, config.step_up_ttl_seconds, detection.detection_id)
    logger.info(
        "Step-up auth required: user_id=%s ttl=%ds (type=%s)",
        detection.user_id, config.step_up_ttl_seconds, detection.detection_type,
    )
    return "step_up_auth"


def _ip_block(redis_client, detection: DetectionEvent, config: RedisConfig) -> str:
    key = ip_block_key(detection.ip_address)
    with observe_redis_latency("setex_ip_block"):
        redis_client.setex(key, config.ip_block_ttl_seconds, detection.detection_id)
    logger.info("IP block set: ip=%s ttl=%ds", detection.ip_address, config.ip_block_ttl_seconds)
    return "ip_block"


def apply_containment(
    detection: DetectionEvent,
    redis_client,
    redis_config: Optional[RedisConfig] = None,
) -> list[str]:
    """Apply containment for a detection and return the list of actions taken."""
    config = redis_config or RedisConfig.from_env()
    dtype = detection.detection_type
    actions: list[str] = []

    if dtype in HARD_QUARANTINE_TYPES:
        actions.append(_quarantine(redis_client, detection, config))

    elif dtype == "credential_stuffing":
        actions.append(_quarantine(redis_client, detection, config))
        scope = detection.details.get("scope", "user")
        if scope == "ip" and detection.ip_address:
            actions.append(_ip_block(redis_client, detection, config))
        elif scope == "ip":
            logger.warning("ip-scope credential_stuffing detection missing ip_address; IP block skipped")

    elif dtype == "geo_velocity_anomaly":
        escalate = is_step_up_pending(redis_client, detection.user_id)
        actions.append(_step_up(redis_client, detection, config))
        if escalate:
            logger.warning(
                "Repeated geo anomaly while step-up pending; escalating user_id=%s to quarantine",
                detection.user_id,
            )
            actions.append(_quarantine(redis_client, detection, config))

    else:
        logger.warning("Unknown detection_type=%s, no containment applied", dtype)

    return actions
