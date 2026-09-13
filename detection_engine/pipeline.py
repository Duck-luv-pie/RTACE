"""Detection pipeline: turns one Kafka record into zero or more DetectionEvents.

Kept free of Kafka consumer plumbing so it can be unit-tested with a fake Redis
and a list-collecting ``emit`` callback.
"""

import logging
from typing import Callable, Optional

from common.enforcement import is_ip_blocked, is_user_quarantined
from common.metrics import (
    credential_stuffing_detections_total,
    events_blocked_total,
    fraud_burst_detections_total,
    geo_velocity_detections_total,
    replay_detections_total,
    transactions_processed_total,
)
from common.models import AuthEvent, DetectionEvent, TransactionEvent
from configs.kafka_config import KafkaConfig
from configs.redis_config import RedisConfig
from detection_engine.cooldown import should_emit
from detection_engine.credential_stuffing_detector import check_credential_stuffing
from detection_engine.fraud_burst_detector import check_fraud_burst
from detection_engine.geo_velocity_detector import check_geo_velocity
from detection_engine.replay_detector import check_replay

logger = logging.getLogger(__name__)

Emit = Callable[[DetectionEvent], None]
EmitAudit = Callable[[dict], None]


def record_ref(topic: str, partition: int, offset: int) -> str:
    """Identity of a Kafka record; stable across redeliveries of the same record."""
    return f"{topic}:{partition}:{offset}"


class DetectionPipeline:
    def __init__(
        self,
        redis_client,
        emit: Emit,
        kafka_config: Optional[KafkaConfig] = None,
        redis_config: Optional[RedisConfig] = None,
        emit_audit: Optional[EmitAudit] = None,
    ) -> None:
        self.redis = redis_client
        self.emit = emit
        self.emit_audit = emit_audit
        self.kafka_config = kafka_config or KafkaConfig.from_env()
        # Loaded once; every detector receives this instance instead of
        # re-reading the environment per event.
        self.redis_config = redis_config or RedisConfig.from_env()

    # ------------------------------------------------------------------ entry

    def handle_record(self, topic: str, partition: int, offset: int, value: dict) -> list[DetectionEvent]:
        """Dispatch on topic. Raises on malformed payloads; the caller decides
        whether that is retried, dead-lettered or dropped."""
        if topic == self.kafka_config.auth_events_topic:
            return self.handle_auth(AuthEvent.model_validate(value))
        return self.handle_transaction(
            TransactionEvent.model_validate(value), record_ref(topic, partition, offset)
        )

    # ---------------------------------------------------------- transactions

    def handle_transaction(
        self, tx: TransactionEvent, ref: Optional[str] = None
    ) -> list[DetectionEvent]:
        if is_user_quarantined(self.redis, tx.user_id):
            self._blocked("transaction", "quarantine", tx.user_id, tx.event_id)
            return []
        cfg = self.redis_config
        candidates = [
            check_replay(tx, self.redis, cfg, record_ref=ref),
            check_geo_velocity(tx, self.redis, cfg),
            check_fraud_burst(tx, self.redis, cfg),
        ]
        emitted = [d for d in candidates if d is not None and self._publish(d)]
        outcome = "detected" if any(candidates) else "clean"
        transactions_processed_total.labels(outcome=outcome).inc()
        return emitted

    # ----------------------------------------------------------------- auth

    def handle_auth(self, auth: AuthEvent) -> list[DetectionEvent]:
        if is_ip_blocked(self.redis, auth.ip_address):
            self._blocked("auth", "ip_block", auth.user_id, auth.event_id, ip=auth.ip_address)
            return []
        det_user, det_ip = check_credential_stuffing(auth, self.redis, self.redis_config)
        return [d for d in (det_user, det_ip) if d is not None and self._publish(d)]

    # ---------------------------------------------------------- enforcement

    def _blocked(self, event_type: str, reason: str, user_id: str, event_id: str, ip: Optional[str] = None) -> None:
        """An active rule rejects this event: it is not analysed, it is refused.

        A quarantined user's transactions must not advance their trusted
        location or burst window, and a blocked IP's login attempts must not
        keep feeding the stuffing counters. The refusal is counted and, when an
        audit sink is configured, recorded so enforcement is observable."""
        events_blocked_total.labels(event_type=event_type, reason=reason).inc()
        if event_type == "transaction":
            transactions_processed_total.labels(outcome="blocked").inc()
        logger.info("Blocked %s: user_id=%s event_id=%s reason=%s", event_type, user_id, event_id, reason)
        if self.emit_audit is not None:
            self.emit_audit(
                {
                    "event": f"{event_type}_blocked",
                    "reason": reason,
                    "user_id": user_id,
                    "event_id": event_id,
                    "ip_address": ip,
                }
            )

    # -------------------------------------------------------------- publish

    def _publish(self, detection: DetectionEvent) -> bool:
        """Count, apply cooldown, and emit. Returns True if the detection went out."""
        self._count(detection)
        if not should_emit(detection, self.redis, self.redis_config):
            logger.debug(
                "Suppressed %s for %s (cooldown)", detection.detection_type, detection.user_id
            )
            return False
        self.emit(detection)
        logger.info(
            "%s: user_id=%s transaction_id=%s detection_id=%s details=%s",
            detection.detection_type,
            detection.user_id,
            detection.transaction_id,
            detection.detection_id,
            detection.details,
        )
        return True

    @staticmethod
    def _count(detection: DetectionEvent) -> None:
        dtype = detection.detection_type
        if dtype == "replay_attack":
            replay_detections_total.labels(detection_type=dtype).inc()
        elif dtype == "geo_velocity_anomaly":
            geo_velocity_detections_total.labels(detection_type=dtype).inc()
        elif dtype == "fraud_burst":
            fraud_burst_detections_total.labels(detection_type=dtype).inc()
        elif dtype == "credential_stuffing":
            credential_stuffing_detections_total.labels(
                scope=detection.details.get("scope", "user")
            ).inc()
