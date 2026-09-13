"""Redis and detector tuning configuration for RTACE."""

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class RedisConfig:
    """Redis connection configuration plus detector windows, thresholds and TTLs.

    Load this once per process (``RedisConfig.from_env()``) and pass it into
    the detectors; do not re-read the environment on the hot path.
    """

    host: str
    port: int
    db: int
    replay_ttl_hours: int
    quarantine_ttl_seconds: int
    session_ttl_days: int
    geo_max_velocity_kmh: float
    burst_window_seconds: int
    burst_threshold: int
    burst_key_ttl_seconds: int
    auth_fail_window_seconds: int
    auth_fail_user_threshold: int
    auth_fail_ip_threshold: int
    auth_fail_key_ttl_seconds: int
    ip_block_ttl_seconds: int
    step_up_ttl_seconds: int
    containment_receipt_ttl_seconds: int
    detection_cooldown_seconds: int

    @classmethod
    def from_env(cls) -> "RedisConfig":
        return cls(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            db=int(os.getenv("REDIS_DB", "0")),
            replay_ttl_hours=int(os.getenv("REDIS_REPLAY_TTL_HOURS", "24")),
            quarantine_ttl_seconds=int(os.getenv("REDIS_QUARANTINE_TTL_SECONDS", "3600")),
            session_ttl_days=int(os.getenv("REDIS_SESSION_TTL_DAYS", "7")),
            geo_max_velocity_kmh=float(os.getenv("GEO_MAX_VELOCITY_KMH", "900")),
            burst_window_seconds=int(os.getenv("REDIS_BURST_WINDOW_SECONDS", "60")),
            burst_threshold=int(os.getenv("REDIS_BURST_THRESHOLD", "20")),
            burst_key_ttl_seconds=int(os.getenv("REDIS_BURST_KEY_TTL_SECONDS", "120")),
            auth_fail_window_seconds=int(os.getenv("REDIS_AUTH_FAIL_WINDOW_SECONDS", "60")),
            auth_fail_user_threshold=int(os.getenv("REDIS_AUTH_FAIL_USER_THRESHOLD", "10")),
            auth_fail_ip_threshold=int(os.getenv("REDIS_AUTH_FAIL_IP_THRESHOLD", "50")),
            auth_fail_key_ttl_seconds=int(os.getenv("REDIS_AUTH_FAIL_KEY_TTL_SECONDS", "120")),
            ip_block_ttl_seconds=int(os.getenv("REDIS_IP_BLOCK_TTL_SECONDS", "3600")),
            step_up_ttl_seconds=int(os.getenv("REDIS_STEP_UP_TTL_SECONDS", "900")),
            # Must outlive Kafka retention so a redelivered detection still finds
            # its receipt and is not re-applied. Default 8 days > 7-day retention.
            containment_receipt_ttl_seconds=int(os.getenv("REDIS_CONTAINMENT_RECEIPT_TTL_SECONDS", str(8 * 24 * 3600))),
            detection_cooldown_seconds=int(os.getenv("DETECTION_COOLDOWN_SECONDS", "60")),
        )
