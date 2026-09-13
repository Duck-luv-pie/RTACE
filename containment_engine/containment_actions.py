"""Apply each detection's containment exactly once, atomically.

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

Detections are delivered at least once (Kafka redelivery, or a producer flush
retry upstream), so applying them must be idempotent PER DETECTION. Plain SETEX
was not: a redelivered detection extended the rule's TTL, and a redelivered geo
anomaly re-read "step-up pending" and wrongly escalated a single anomaly to a
quarantine. Each detection now writes a receipt (``containment:applied:{id}``,
via detection ids that are stable across redelivery); the receipt and every
rule write share one WATCH/MULTI/EXEC transaction. A redelivery finds the
receipt and returns the original actions, changing nothing. WATCH on the
step-up key also serialises two distinct geo detections for one user, so the
escalation decision cannot race.
"""

import json
import logging
from typing import Optional

from redis.exceptions import WatchError

from common.enforcement import ip_block_key, quarantine_key, step_up_key
from common.metrics import observe_redis_latency
from common.models import DetectionEvent
from configs.redis_config import RedisConfig

logger = logging.getLogger(__name__)

HARD_QUARANTINE_TYPES = ("replay_attack", "fraud_burst")
RECEIPT_PREFIX = "containment:applied:"


def _plan(pipe, detection: DetectionEvent, config: RedisConfig) -> tuple[list[str], list[tuple[str, int]]]:
    """Decide the actions and the (key, ttl) writes for this detection.

    ``pipe`` is WATCHing the step-up key, so reads here are consistent with the
    MULTI/EXEC that follows. Returns (actions, writes).
    """
    dtype = detection.detection_type
    actions: list[str] = []
    writes: list[tuple[str, int]] = []

    if dtype in HARD_QUARANTINE_TYPES:
        actions.append("quarantine")
        writes.append((quarantine_key(detection.user_id), config.quarantine_ttl_seconds))

    elif dtype == "credential_stuffing":
        actions.append("quarantine")
        writes.append((quarantine_key(detection.user_id), config.quarantine_ttl_seconds))
        scope = detection.details.get("scope", "user")
        if scope == "ip" and detection.ip_address:
            actions.append("ip_block")
            writes.append((ip_block_key(detection.ip_address), config.ip_block_ttl_seconds))
        elif scope == "ip":
            logger.warning("ip-scope credential_stuffing detection missing ip_address; IP block skipped")

    elif dtype == "geo_velocity_anomaly":
        actions.append("step_up_auth")
        writes.append((step_up_key(detection.user_id), config.step_up_ttl_seconds))
        if pipe.exists(step_up_key(detection.user_id)):  # a step-up is already pending
            logger.warning(
                "Repeated geo anomaly while step-up pending; escalating user_id=%s to quarantine",
                detection.user_id,
            )
            actions.append("quarantine")
            writes.append((quarantine_key(detection.user_id), config.quarantine_ttl_seconds))

    else:
        logger.warning("Unknown detection_type=%s, no containment applied", dtype)

    return actions, writes


def apply_containment(
    detection: DetectionEvent,
    redis_client,
    redis_config: Optional[RedisConfig] = None,
) -> list[str]:
    """Apply containment once for this detection id and return the actions taken.

    A redelivery of the same detection returns the original actions without
    touching any rule. On a WATCH conflict (a concurrent geo detection for the
    same user) the transaction retries.
    """
    config = redis_config or RedisConfig.from_env()
    receipt = f"{RECEIPT_PREFIX}{detection.detection_id}"
    guard = step_up_key(detection.user_id)  # watched so geo escalation cannot race

    with observe_redis_latency("apply_containment"):
        while True:
            with redis_client.pipeline() as pipe:
                try:
                    pipe.watch(receipt, guard)
                    prior = pipe.get(receipt)
                    if prior is not None:
                        return json.loads(prior)

                    actions, writes = _plan(pipe, detection, config)
                    if not actions:  # unknown type: a true no-op, no receipt needed
                        pipe.unwatch()
                        return []

                    pipe.multi()
                    pipe.set(receipt, json.dumps(actions), ex=config.containment_receipt_ttl_seconds)
                    for key, ttl in writes:
                        pipe.setex(key, ttl, detection.detection_id)
                    pipe.execute()
                    if actions:
                        logger.info(
                            "Containment applied: user_id=%s detection_id=%s actions=%s",
                            detection.user_id, detection.detection_id, actions,
                        )
                    return actions
                except WatchError:
                    continue
