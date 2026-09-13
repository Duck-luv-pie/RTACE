"""Detection engine: consume tx-events and auth-events, run detectors, publish detections."""

import logging
import os
import threading
import time

from kafka.errors import KafkaError
from prometheus_client import start_http_server

from common.consumer_loop import LoopSettings, install_signal_handlers, run_consumer_loop
from common.kafka_client import create_consumer, create_producer, flush_and_verify, send_message
from common.metrics import detection_pipeline_latency_seconds
from common.models import DetectionEvent
from common.redis_client import create_redis_client
from configs.kafka_config import KafkaConfig
from configs.redis_config import RedisConfig
from detection_engine.pipeline import DetectionPipeline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

METRICS_PORT = int(os.getenv("DETECTION_METRICS_PORT", "9091"))
STAGE = "detection"


def run_detection_engine() -> None:
    """Consume tx-events and auth-events, run detectors, produce to detections topic."""
    start_http_server(METRICS_PORT)
    logger.info("Metrics exposed on port %s", METRICS_PORT)

    kafka_config = KafkaConfig.from_env()
    redis_config = RedisConfig.from_env()

    consumer = create_consumer(
        [kafka_config.tx_events_topic, kafka_config.auth_events_topic],
        group_id="rtace-detection-engine",
        config=kafka_config,
    )
    producer = create_producer(kafka_config)
    redis_client = create_redis_client(redis_config)

    def emit(detection: DetectionEvent) -> None:
        send_message(producer, kafka_config.detections_topic, detection.model_dump(mode="json"), key=detection.user_id)

    def emit_audit(audit: dict) -> None:
        send_message(producer, kafka_config.audit_log_topic, audit, key=audit["user_id"])

    pipeline = DetectionPipeline(redis_client, emit, kafka_config, redis_config, emit_audit=emit_audit)

    def handle_batch(records):
        start = time.perf_counter()
        try:
            result = pipeline.handle_batch(records)
            # Durability gate for cooled-down detections: a detection is emitted
            # AND its cooldown claimed in the same batch. Confirm delivery before
            # keeping the cooldown; if delivery fails, release the cooldown so the
            # retried batch re-emits the detection instead of suppressing it.
            try:
                flush_and_verify(producer)
            except KafkaError:
                pipeline.release_cooldown(result.claimed_cooldown_keys)
                raise  # transient: run_consumer_loop retries the batch, does not commit
            return result.failures
        finally:
            # Per-record latency: batch wall time spread over the records in it.
            elapsed = time.perf_counter() - start
            per_record = elapsed / max(len(records), 1)
            for _ in records:
                detection_pipeline_latency_seconds.observe(per_record)

    shutdown = threading.Event()
    install_signal_handlers(shutdown)

    logger.info(
        "Detection engine started: consume %s + %s → %s (DLQ %s)",
        kafka_config.tx_events_topic, kafka_config.auth_events_topic,
        kafka_config.detections_topic, kafka_config.dlq_topic,
    )
    run_consumer_loop(
        consumer,
        handle_batch,
        LoopSettings(stage=STAGE, poll_timeout_ms=kafka_config.poll_timeout_ms),
        shutdown,
        producer=producer,
        dlq_topic=kafka_config.dlq_topic,
    )


if __name__ == "__main__":
    run_detection_engine()
