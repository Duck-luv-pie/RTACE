"""Per-subject detection cooldown.

Window-based detectors (fraud burst, credential stuffing) stay over their
threshold for as long as the attack continues, so without suppression every
further event produces another detection, another containment write and
another audit record. The cooldown lets the first detection for a given
(detection_type, subject) through and suppresses repeats for
``detection_cooldown_seconds``. Containment is idempotent, so nothing is lost
by suppressing: the subject is already quarantined / blocked.

Implemented as ``SET key 1 NX EX cooldown``: exactly one caller wins per
window, even across several detection-engine processes. The batch form
issues every SET in one pipelined round trip.
"""

from typing import Optional, Sequence

from common.metrics import detections_suppressed_total, observe_redis_latency
from common.models import DetectionEvent
from configs.redis_config import RedisConfig

COOLDOWN_KEY_PREFIX = "cooldown:"


def cooldown_subject(detection: DetectionEvent) -> str:
    """The entity a detection is about: the IP for IP-scope detections, else the user."""
    if detection.details.get("scope") == "ip" and detection.ip_address:
        return f"ip:{detection.ip_address.replace(':', '-')}"
    return f"user:{detection.user_id}"


def cooldown_key(detection: DetectionEvent) -> str:
    return f"{COOLDOWN_KEY_PREFIX}{detection.detection_type}:{cooldown_subject(detection)}"


def filter_cooled_down(
    detections: Sequence[DetectionEvent],
    redis_client,
    redis_config: Optional[RedisConfig] = None,
) -> tuple[list[DetectionEvent], list[str]]:
    """Reserve the cooldown for each first-in-window detection.

    Returns ``(allowed, claimed_keys)``: the detections that won their cooldown
    (and so may be emitted), and the cooldown keys this call set. The keys are
    returned so the caller can *release* them with ``release_cooldown`` if the
    detections are not durably delivered — otherwise a cooldown claimed on an
    attempt whose Kafka produce failed would suppress the detection on retry and
    lose it. Cooldown keys are committed (kept) only once delivery succeeds.
    """
    config = redis_config or RedisConfig.from_env()
    if not detections:
        return [], []
    if config.detection_cooldown_seconds <= 0:
        return list(detections), []

    with observe_redis_latency("cooldown_batch"):
        with redis_client.pipeline(transaction=False) as pipe:
            for d in detections:
                pipe.set(cooldown_key(d), d.detection_id, nx=True, ex=config.detection_cooldown_seconds)
            acquired = pipe.execute()

    allowed, claimed = [], []
    for d, ok in zip(detections, acquired):
        if ok:
            allowed.append(d)
            claimed.append(cooldown_key(d))
        else:
            detections_suppressed_total.labels(detection_type=d.detection_type).inc()
    return allowed, claimed


def release_cooldown(keys: Sequence[str], redis_client) -> None:
    """Delete cooldown keys claimed for detections that were not delivered, so a
    retry can re-claim and re-emit them."""
    if keys:
        redis_client.delete(*keys)


def should_emit(detection: DetectionEvent, redis_client, redis_config: Optional[RedisConfig] = None) -> bool:
    """Single-detection convenience wrapper around filter_cooled_down."""
    return bool(filter_cooled_down([detection], redis_client, redis_config)[0])
