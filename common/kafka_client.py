"""Shared Kafka producer/consumer utilities for RTACE."""

import json
import logging
from typing import Any, Optional, Union

from kafka import KafkaProducer, KafkaConsumer
from kafka.errors import KafkaError

from configs.kafka_config import KafkaConfig

logger = logging.getLogger(__name__)


def _on_send_error(exc: Exception) -> None:
    logger.error("Async Kafka send failed: %s", exc)


def create_producer(config: Optional[KafkaConfig] = None) -> KafkaProducer:
    """Create a Kafka producer with JSON serialization and throughput-optimised defaults.

    linger_ms / batch_size: accumulate messages for up to 5 ms so the broker
    receives large batches instead of one frame per message.
    compression_type: lz4 is fast enough that it doesn't add CPU latency but
    halves network + broker I/O at high rates.
    acks=1: leader-ack only; sufficient for a fraud detection pipeline where
    replay-detection handles duplicates anyway.
    """
    cfg = config or KafkaConfig.from_env()
    return KafkaProducer(
        bootstrap_servers=cfg.bootstrap_servers,
        value_serializer=lambda v: json.dumps(v, default=str).encode("utf-8"),
        key_serializer=lambda k: k.encode("utf-8") if k else None,
        linger_ms=5,
        batch_size=65_536,
        compression_type="lz4",
        acks=1,
        retries=3,
        max_in_flight_requests_per_connection=5,
    )


def create_consumer(
    topic: Union[str, list[str]],
    group_id: str,
    config: Optional[KafkaConfig] = None,
) -> KafkaConsumer:
    """Create a Kafka consumer for one or more topics.

    max_poll_records: pull up to 500 messages per poll instead of the default
    500 (already the default in newer kafka-python, explicit here for clarity).
    fetch_min_bytes / fetch_max_wait_ms: don't return a fetch response until
    there is at least 64 KB of data available, or 100 ms have elapsed.  This
    dramatically reduces the number of empty-poll round trips at high load.
    """
    cfg = config or KafkaConfig.from_env()
    topics = [topic] if isinstance(topic, str) else topic
    return KafkaConsumer(
        *topics,
        bootstrap_servers=cfg.bootstrap_servers,
        group_id=group_id,
        auto_offset_reset="earliest",
        enable_auto_commit=True,
        value_deserializer=lambda m: json.loads(m.decode("utf-8")) if m else None,
        max_poll_records=500,
        fetch_min_bytes=65_536,
        fetch_max_wait_ms=100,
    )


def send_message(
    producer: KafkaProducer,
    topic: str,
    value: dict[str, Any],
    key: Optional[str] = None,
) -> None:
    """Enqueue a message for async delivery.

    The producer batches and sends in a background I/O thread; we never block
    the consumer loop waiting for a broker ack.  Errors are surfaced via the
    _on_send_error callback so they appear in logs without stalling throughput.
    Call producer.flush() on graceful shutdown to drain the internal buffer.
    """
    try:
        producer.send(topic, value=value, key=key).add_errback(_on_send_error)
    except KafkaError as e:
        logger.error("Failed to enqueue message to %s: %s", topic, e)
