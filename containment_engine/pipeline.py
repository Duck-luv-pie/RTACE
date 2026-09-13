"""Containment pipeline: turns one DetectionEvent into Redis enforcement + an audit record."""

import logging
from typing import Callable, Optional

from common.metrics import containment_actions_total
from common.models import DetectionEvent
from configs.redis_config import RedisConfig
from containment_engine.containment_actions import apply_containment

logger = logging.getLogger(__name__)

EmitAudit = Callable[[dict], None]


class ContainmentPipeline:
    def __init__(
        self,
        redis_client,
        emit_audit: EmitAudit,
        redis_config: Optional[RedisConfig] = None,
    ) -> None:
        self.redis = redis_client
        self.emit_audit = emit_audit
        self.redis_config = redis_config or RedisConfig.from_env()

    def handle_record(self, topic: str, partition: int, offset: int, value: dict) -> None:
        self.handle_detection(DetectionEvent.model_validate(value))

    def handle_detection(self, detection: DetectionEvent) -> list[str]:
        actions = apply_containment(detection, self.redis, self.redis_config)
        for action in actions:
            containment_actions_total.labels(
                detection_type=detection.detection_type, action=action
            ).inc()
        self.emit_audit(
            {
                "detection_id": detection.detection_id,
                "detection_type": detection.detection_type,
                "user_id": detection.user_id,
                "ip_address": detection.ip_address,
                "actions": actions,
                "action": "+".join(actions) if actions else "none",
                "timestamp": detection.timestamp.isoformat(),
                "details": detection.details,
            }
        )
        logger.info(
            "Containment applied and audited: user_id=%s detection_id=%s actions=%s",
            detection.user_id,
            detection.detection_id,
            actions,
        )
        return actions
