"""Containment engine: consume detections, apply containment actions, optionally audit-log."""

import logging

from prometheus_client import start_http_server

from common.kafka_client import create_consumer, create_producer, send_message
from common.metrics import containment_actions_total
from common.models import DetectionEvent
from common.redis_client import create_redis_client
from configs.kafka_config import KafkaConfig
from containment_engine.containment_actions import apply_containment

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

METRICS_PORT = 9093


def run_containment_engine() -> None:
    """Consume detections, apply containment, optionally write to audit-log."""
    start_http_server(METRICS_PORT)
    logger.info("Metrics exposed on port %s", METRICS_PORT)

    kafka_config = KafkaConfig.from_env()
    consumer = create_consumer(
        kafka_config.detections_topic,
        group_id="rtace-containment-engine",
        config=kafka_config,
    )
    producer = create_producer(kafka_config)
    redis_client = create_redis_client()

    logger.info(
        "Containment engine started: consume %s → Redis + %s",
        kafka_config.detections_topic,
        kafka_config.audit_log_topic,
    )

    for message in consumer:
        try:
            raw = message.value
            if not raw:
                continue
            detection = DetectionEvent.model_validate(raw)
            apply_containment(detection, redis_client)
            containment_actions_total.labels(
                detection_type=detection.detection_type,
                action="quarantine",
            ).inc()
            # Audit log: record that we took action
            audit = {
                "detection_id": detection.detection_id,
                "detection_type": detection.detection_type,
                "user_id": detection.user_id,
                "action": "quarantine",
                "timestamp": detection.timestamp.isoformat(),
            }
            send_message(
                producer,
                kafka_config.audit_log_topic,
                audit,
                key=detection.user_id,
            )
            logger.info(
                "Containment applied and audited: user_id=%s detection_id=%s",
                detection.user_id,
                detection.detection_id,
            )
        except Exception as e:
            logger.exception("Error processing detection: %s", e)

    consumer.close()
    producer.close()


if __name__ == "__main__":
    run_containment_engine()
