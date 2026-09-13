"""Shared Kafka producer/consumer utilities for RTACE."""

import json
import logging
from typing import Any, Optional, Union

from kafka import ConsumerRebalanceListener, KafkaConsumer, KafkaProducer
from kafka.errors import KafkaError

from common.metrics import kafka_send_failures_total
from configs.kafka_config import KafkaConfig

logger = logging.getLogger(__name__)


def create_producer(config: Optional[KafkaConfig] = None) -> KafkaProducer:
    """Create a Kafka producer with JSON serialization and safe, batched defaults.

    linger_ms / batch_size: accumulate messages for up to 5 ms so the broker
    receives batches instead of one frame per message.
    compression_type: lz4 halves network + broker I/O at high rates.
    acks="all" + enable_idempotence: a detection or audit record is only
    considered sent once the in-sync replicas have it, and broker-side
    de-duplication means a retried batch cannot produce duplicates or reorder
    records within a partition. kafka-python requires
    max_in_flight_requests_per_connection=1 for idempotence (the Java client
    allows 5); throughput still comes from linger/batching, not pipelining.
    """
    cfg = config or KafkaConfig.from_env()
    return KafkaProducer(
        bootstrap_servers=cfg.bootstrap_servers,
        value_serializer=lambda v: json.dumps(v, default=str).encode("utf-8"),
        key_serializer=lambda k: k.encode("utf-8") if k else None,
        linger_ms=5,
        batch_size=65_536,
        compression_type="lz4",
        acks="all",
        enable_idempotence=True,
        max_in_flight_requests_per_connection=1,
    )


def create_consumer(
    topic: Union[str, list[str]],
    group_id: str,
    config: Optional[KafkaConfig] = None,
    rebalance_listener: Optional[ConsumerRebalanceListener] = None,
) -> KafkaConsumer:
    """Create a Kafka consumer for one or more topics.

    Offsets are committed manually by the consumer loop *after* a record has
    been processed (see common.consumer_loop), never by a timer. Values are
    returned as raw bytes: decoding happens in the loop so that an undecodable
    record can be dead-lettered instead of raising inside poll() and wedging
    the partition.

    rebalance_listener: optional ConsumerRebalanceListener notified when this
    process gains or loses partitions (used to drop per-process caches).
    """
    cfg = config or KafkaConfig.from_env()
    topics = [topic] if isinstance(topic, str) else topic
    consumer = KafkaConsumer(
        bootstrap_servers=cfg.bootstrap_servers,
        group_id=group_id,
        auto_offset_reset="earliest",
        enable_auto_commit=False,
        value_deserializer=None,
        max_poll_records=cfg.max_poll_records,
        fetch_min_bytes=cfg.fetch_min_bytes,
        fetch_max_wait_ms=cfg.fetch_max_wait_ms,
    )
    consumer.subscribe(topics, listener=rebalance_listener)
    return consumer


def decode_json(raw: Optional[bytes]) -> Any:
    """Decode a record value. Raises ValueError on malformed input; returns None for a tombstone."""
    if raw is None or raw == b"":
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"undecodable record value: {e}") from e


def send_message(
    producer: KafkaProducer,
    topic: str,
    value: dict[str, Any],
    key: Optional[str] = None,
) -> None:
    """Enqueue a message for async, batched delivery.

    The producer batches and sends in a background I/O thread, so the consumer
    loop stays fast. Durability comes from calling ``flush_and_verify`` before
    committing input offsets: a send that fails is recorded on the producer
    (``_rtace_last_error``) and surfaced there, so the batch is retried instead
    of committed. Failures are also counted in ``kafka_send_failures_total``.
    """

    def _on_error(exc: Exception) -> None:
        kafka_send_failures_total.labels(topic=topic).inc()
        # Latch the error so flush_and_verify can refuse to commit the batch.
        producer._rtace_last_error = exc  # type: ignore[attr-defined]
        logger.error("Async Kafka send to %s failed: %s", topic, exc)

    try:
        producer.send(topic, value=value, key=key).add_errback(_on_error)
    except KafkaError as e:
        kafka_send_failures_total.labels(topic=topic).inc()
        producer._rtace_last_error = e  # type: ignore[attr-defined]
        logger.error("Failed to enqueue message to %s: %s", topic, e)


def reset_send_errors(producer: KafkaProducer) -> None:
    """Clear any latched send error before a fresh batch of sends."""
    producer._rtace_last_error = None  # type: ignore[attr-defined]


def flush_and_verify(producer: KafkaProducer, timeout: Optional[float] = None) -> None:
    """Block until every buffered send is acknowledged, then raise if any failed.

    flush() waits for all in-flight futures (and therefore their error
    callbacks) to complete, so after it returns ``_rtace_last_error`` reflects
    the whole batch. Raising here lets the consumer loop retry the batch rather
    than commit input offsets for detections that were never durably delivered
    (end-to-end at-least-once). Retries may duplicate; every downstream write is
    idempotent (SETEX enforcement, event-id sorted-set members, stable keys).
    """
    producer.flush(timeout=timeout)
    err = getattr(producer, "_rtace_last_error", None)
    if err is not None:
        producer._rtace_last_error = None  # type: ignore[attr-defined]
        raise KafkaError(f"one or more sends in this batch failed: {err}")
