"""Detection engine: consume tx-events and auth-events, run detectors, publish detections."""

import logging
import os
import threading
import time

from kafka import ConsumerRebalanceListener
from prometheus_client import start_http_server

from common import session_cache
from common.consumer_loop import LoopSettings, install_signal_handlers, run_consumer_loop
from common.kafka_client import create_consumer, create_producer, send_message
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


class _SessionCacheInvalidator(ConsumerRebalanceListener):
    """Drop the in-process session cache whenever partitions are taken away.

    After a rebalance another process may have advanced a user's session in
    Redis; anything this process cached before the revocation is suspect.
    """

    def on_partitions_revoked(self, revoked):
        if revoked:
            logger.info("Partitions revoked (%d); clearing L1 session cache", len(revoked))
            session_cache.clear()

    def on_partitions_assigned(self, assigned):
        logger.info("Partitions assigned: %d", len(assigned))


def run_detection_engine() -> None:
    """Consume tx-events and auth-events, run detectors, produce to detections topic."""
    start_http_server(METRICS_PORT)
    logger.info("Metrics exposed on port %s", METRICS_PORT)

    kafka_config = KafkaConfig.from_env()
    redis_config = RedisConfig.from_env()
    session_cache.configure(
        redis_config.session_cache_maxsize, redis_config.session_cache_ttl_seconds
    )

    consumer = create_consumer(
        [kafka_config.tx_events_topic, kafka_config.auth_events_topic],
        group_id="rtace-detection-engine",
        config=kafka_config,
        rebalance_listener=_SessionCacheInvalidator(),
    )
    producer = create_producer(kafka_config)
    redis_client = create_redis_client(redis_config)

    def emit(detection: DetectionEvent) -> None:
        send_message(
            producer,
            kafka_config.detections_topic,
            detection.model_dump(mode="json"),
            key=detection.user_id,
        )

    def emit_audit(audit: dict) -> None:
        send_message(producer, kafka_config.audit_log_topic, audit, key=audit["user_id"])

    pipeline = DetectionPipeline(
        redis_client, emit, kafka_config, redis_config, emit_audit=emit_audit
    )

    def handle(topic: str, partition: int, offset: int, value) -> None:
        start = time.perf_counter()
        try:
            pipeline.handle_record(topic, partition, offset, value)
        finally:
            detection_pipeline_latency_seconds.observe(time.perf_counter() - start)

    shutdown = threading.Event()
    install_signal_handlers(shutdown)

    logger.info(
        "Detection engine started: consume %s + %s → %s (DLQ %s)",
        kafka_config.tx_events_topic,
        kafka_config.auth_events_topic,
        kafka_config.detections_topic,
        kafka_config.dlq_topic,
    )
    run_consumer_loop(
        consumer,
        handle,
        LoopSettings(stage=STAGE, poll_timeout_ms=kafka_config.poll_timeout_ms),
        shutdown,
        producer=producer,
        dlq_topic=kafka_config.dlq_topic,
    )


if __name__ == "__main__":
    run_detection_engine()
