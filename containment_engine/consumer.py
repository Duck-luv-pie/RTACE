"""Containment engine: consume detections, apply containment actions, write audit-log."""

import logging
import threading

from prometheus_client import start_http_server

from common.consumer_loop import LoopSettings, install_signal_handlers, run_consumer_loop
from common.kafka_client import create_consumer, create_producer, send_message
from common.redis_client import create_redis_client
from configs.kafka_config import KafkaConfig
from configs.redis_config import RedisConfig
from containment_engine.pipeline import ContainmentPipeline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

METRICS_PORT = 9093
STAGE = "containment"


def run_containment_engine() -> None:
    """Consume detections, apply containment, write to audit-log."""
    start_http_server(METRICS_PORT)
    logger.info("Metrics exposed on port %s", METRICS_PORT)

    kafka_config = KafkaConfig.from_env()
    redis_config = RedisConfig.from_env()
    consumer = create_consumer(
        kafka_config.detections_topic,
        group_id="rtace-containment-engine",
        config=kafka_config,
    )
    producer = create_producer(kafka_config)
    redis_client = create_redis_client(redis_config)

    def emit_audit(audit: dict) -> None:
        send_message(producer, kafka_config.audit_log_topic, audit, key=audit["user_id"])

    pipeline = ContainmentPipeline(redis_client, emit_audit, redis_config)

    shutdown = threading.Event()
    install_signal_handlers(shutdown)

    logger.info(
        "Containment engine started: consume %s → Redis + %s (DLQ %s)",
        kafka_config.detections_topic,
        kafka_config.audit_log_topic,
        kafka_config.dlq_topic,
    )
    run_consumer_loop(
        consumer,
        pipeline.handle_record,
        LoopSettings(stage=STAGE, poll_timeout_ms=kafka_config.poll_timeout_ms),
        shutdown,
        producer=producer,
        dlq_topic=kafka_config.dlq_topic,
    )


if __name__ == "__main__":
    run_containment_engine()
