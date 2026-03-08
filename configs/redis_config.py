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

    @classmethod
    def from_env(cls) -> "RedisConfig":
        return cls(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            db=int(os.getenv("REDIS_DB", "0")),
            replay_ttl_hours=int(os.getenv("REDIS_REPLAY_TTL_HOURS", "24")),
            quarantine_ttl_seconds=int(os.getenv("REDIS_QUARANTINE_TTL_SECONDS", "3600")),
        )
