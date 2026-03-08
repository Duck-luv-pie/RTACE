"""Shared Kafka producer/consumer utilities for RTACE."""

import json
import logging
from typing import Any, Optional

from kafka import KafkaProducer, KafkaConsumer
from kafka.errors import KafkaError

from configs.kafka_config import KafkaConfig

logger = logging.getLogger(__name__)


def create_producer(config: Optional[KafkaConfig] = None) -> KafkaProducer:
    """Create a Kafka producer with JSON serialization."""
    cfg = config or KafkaConfig.from_env()
    return KafkaProducer(
        bootstrap_servers=cfg.bootstrap_servers,
        value_serializer=lambda v: json.dumps(v, default=str).encode("utf-8"),
        key_serializer=lambda k: k.encode("utf-8") if k else None,
    )


def create_consumer(
    topic: str,
    group_id: str,
    config: Optional[KafkaConfig] = None,
) -> KafkaConsumer:
    """Create a Kafka consumer for a topic."""
    cfg = config or KafkaConfig.from_env()
    return KafkaConsumer(
        topic,
        bootstrap_servers=cfg.bootstrap_servers,
        group_id=group_id,
        auto_offset_reset="earliest",
        enable_auto_commit=True,
        value_deserializer=lambda m: json.loads(m.decode("utf-8")) if m else None,
    )


def send_message(
    producer: KafkaProducer,
    topic: str,
    value: dict[str, Any],
    key: Optional[str] = None,
) -> None:
    """Send a message to a topic. Blocks until acknowledged."""
    future = producer.send(topic, value=value, key=key)
    try:
        future.get(timeout=10)
    except KafkaError as e:
        logger.exception("Failed to send message to %s: %s", topic, e)
        raise
