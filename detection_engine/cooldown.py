"""Per-subject detection cooldown.

Window-based detectors (fraud burst, credential stuffing) stay over their
threshold for as long as the attack continues, so without suppression every
further event produces another detection, another containment write and
another audit record. The cooldown lets the first detection for a given
(detection_type, subject) through and suppresses repeats for
``detection_cooldown_seconds``. Containment is idempotent, so nothing is lost
by suppressing: the subject is already quarantined / blocked.
"""

from typing import Optional

from common.metrics import detections_suppressed_total, observe_redis_latency
from common.models import DetectionEvent
from configs.redis_config import RedisConfig

COOLDOWN_KEY_PREFIX = "cooldown:"


def cooldown_subject(detection: DetectionEvent) -> str:
    """The entity a detection is about: the IP for IP-scope detections, else the user."""
    if detection.details.get("scope") == "ip" and detection.ip_address:
        return f"ip:{detection.ip_address.replace(':', '-')}"
    return f"user:{detection.user_id}"


def should_emit(
    detection: DetectionEvent,
    redis_client,
    redis_config: Optional[RedisConfig] = None,
) -> bool:
    """Return True if no detection of this type for this subject was emitted recently.

    Implemented as ``SET key 1 NX EX cooldown``: exactly one caller wins per
    window, even across several detection-engine processes. A cooldown of 0
    disables suppression.
    """
    config = redis_config or RedisConfig.from_env()
    if config.detection_cooldown_seconds <= 0:
        return True

    key = f"{COOLDOWN_KEY_PREFIX}{detection.detection_type}:{cooldown_subject(detection)}"
    with observe_redis_latency("cooldown_check"):
        acquired = redis_client.set(key, "1", nx=True, ex=config.detection_cooldown_seconds)
    if acquired:
        return True
    detections_suppressed_total.labels(detection_type=detection.detection_type).inc()
    return False
