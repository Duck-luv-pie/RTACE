"""Redis configuration for RTACE."""

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class RedisConfig:
    """Redis connection configuration."""

    host: str
    port: int
    db: int
    replay_ttl_hours: int
    quarantine_ttl_seconds: int
    session_ttl_days: int
    burst_window_seconds: int
    burst_threshold: int
    burst_key_ttl_seconds: int

    @classmethod
    def from_env(cls) -> "RedisConfig":
        return cls(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            db=int(os.getenv("REDIS_DB", "0")),
            replay_ttl_hours=int(os.getenv("REDIS_REPLAY_TTL_HOURS", "24")),
            quarantine_ttl_seconds=int(os.getenv("REDIS_QUARANTINE_TTL_SECONDS", "3600")),
            session_ttl_days=int(os.getenv("REDIS_SESSION_TTL_DAYS", "7")),
            burst_window_seconds=int(os.getenv("REDIS_BURST_WINDOW_SECONDS", "60")),
            burst_threshold=int(os.getenv("REDIS_BURST_THRESHOLD", "20")),
            burst_key_ttl_seconds=int(os.getenv("REDIS_BURST_KEY_TTL_SECONDS", "120")),
        )
