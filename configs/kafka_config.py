"""Kafka configuration for RTACE."""

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class KafkaConfig:
    """Kafka connection and topic configuration."""

    bootstrap_servers: str
    tx_events_topic: str
    auth_events_topic: str
    detections_topic: str
    audit_log_topic: str

    @classmethod
    def from_env(cls) -> "KafkaConfig":
        return cls(
            bootstrap_servers=os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"),
            tx_events_topic=os.getenv("KAFKA_TX_EVENTS_TOPIC", "tx-events"),
            auth_events_topic=os.getenv("KAFKA_AUTH_EVENTS_TOPIC", "auth-events"),
            detections_topic=os.getenv("KAFKA_DETECTIONS_TOPIC", "detections"),
            audit_log_topic=os.getenv("KAFKA_AUDIT_LOG_TOPIC", "audit-log"),
        )
