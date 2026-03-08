"""Detection engine: consume tx-events, run replay detection, publish detections."""

import logging
import time

from prometheus_client import start_http_server

from common.kafka_client import create_consumer, create_producer, send_message
from common.metrics import (
    detection_pipeline_latency_seconds,
    replay_detections_total,
    transactions_processed_total,
)
from common.models import TransactionEvent, DetectionEvent
from common.redis_client import create_redis_client
from configs.kafka_config import KafkaConfig
from detection_engine.replay_detector import check_replay

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

METRICS_PORT = 9091


def run_detection_engine() -> None:
    """Consume tx-events, run replay detection, produce to detections topic."""
    start_http_server(METRICS_PORT)
    logger.info("Metrics exposed on port %s", METRICS_PORT)

    kafka_config = KafkaConfig.from_env()
    consumer = create_consumer(
        kafka_config.tx_events_topic,
        group_id="rtace-detection-engine",
        config=kafka_config,
    )
    producer = create_producer(kafka_config)
    redis_client = create_redis_client()

    logger.info(
        "Detection engine started: consume %s → produce %s",
        kafka_config.tx_events_topic,
        kafka_config.detections_topic,
    )

    for message in consumer:
        start = time.perf_counter()
        try:
            raw = message.value
            if not raw:
                continue
            tx = TransactionEvent.model_validate(raw)
            detection = check_replay(tx, redis_client)
            if detection:
                transactions_processed_total.labels(status="replay").inc()
                replay_detections_total.labels(detection_type=detection.detection_type).inc()
                payload = detection.model_dump(mode="json")
                send_message(
                    producer,
                    kafka_config.detections_topic,
                    payload,
                    key=detection.user_id,
                )
                logger.info(
                    "Replay detected: user_id=%s transaction_id=%s detection_id=%s",
                    detection.user_id,
                    detection.transaction_id,
                    detection.detection_id,
                )
            else:
                transactions_processed_total.labels(status="ok").inc()
            detection_pipeline_latency_seconds.observe(time.perf_counter() - start)
        except Exception as e:
            logger.exception("Error processing message: %s", e)
            detection_pipeline_latency_seconds.observe(time.perf_counter() - start)

    consumer.close()
    producer.close()


if __name__ == "__main__":
    run_detection_engine()
