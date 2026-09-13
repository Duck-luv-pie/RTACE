"""Kafka configuration for RTACE."""

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class KafkaConfig:
    """Kafka connection, topic and consumer-tuning configuration."""

    bootstrap_servers: str
    tx_events_topic: str
    auth_events_topic: str
    detections_topic: str
    audit_log_topic: str
    dlq_topic: str
    # Consumer fetch tuning. fetch_min_bytes=1 returns records as soon as any
    # are available (lowest latency); raise it together with fetch_max_wait_ms
    # to trade latency for fewer, larger fetches under heavy load.
    fetch_min_bytes: int
    fetch_max_wait_ms: int
    max_poll_records: int
    poll_timeout_ms: int

    @classmethod
    def from_env(cls) -> "KafkaConfig":
        return cls(
            bootstrap_servers=os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"),
            tx_events_topic=os.getenv("KAFKA_TX_EVENTS_TOPIC", "tx-events"),
            auth_events_topic=os.getenv("KAFKA_AUTH_EVENTS_TOPIC", "auth-events"),
            detections_topic=os.getenv("KAFKA_DETECTIONS_TOPIC", "detections"),
            audit_log_topic=os.getenv("KAFKA_AUDIT_LOG_TOPIC", "audit-log"),
            dlq_topic=os.getenv("KAFKA_DLQ_TOPIC", "rtace-dlq"),
            fetch_min_bytes=int(os.getenv("KAFKA_FETCH_MIN_BYTES", "1")),
            fetch_max_wait_ms=int(os.getenv("KAFKA_FETCH_MAX_WAIT_MS", "100")),
            max_poll_records=int(os.getenv("KAFKA_MAX_POLL_RECORDS", "500")),
            poll_timeout_ms=int(os.getenv("KAFKA_POLL_TIMEOUT_MS", "1000")),
        )
