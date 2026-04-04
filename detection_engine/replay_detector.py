"""Replay attack detection: same transaction hash seen within the last 24 hours."""

from datetime import datetime, timezone
from typing import Optional

from common.metrics import observe_redis_latency
from common.models import TransactionEvent, DetectionEvent
from common.redis_client import create_redis_client
from configs.redis_config import RedisConfig

REDIS_KEY_PREFIX = "replay:seen:"


def _date_key() -> str:
    """Key suffix by date so we can expire per-day (e.g. replay:seen:2025-03-08)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def check_replay(
    tx: TransactionEvent,
    redis_client,
    redis_config: Optional[RedisConfig] = None,
) -> Optional[DetectionEvent]:
    """
    Check if this transaction is a replay (hash already seen in Redis within 24h).
    If not seen, add hash to Redis and return None.
    If seen, return a DetectionEvent.
    """
    config = redis_config or RedisConfig.from_env()
    key = f"{REDIS_KEY_PREFIX}{_date_key()}"
    tx_hash = tx.replay_hash()

    with observe_redis_latency("replay_check"):
        with redis_client.pipeline(transaction=False) as pipe:
            pipe.sadd(key, tx_hash)
            pipe.expire(key, config.replay_ttl_hours * 3600)
            added, _ = pipe.execute()

    if added:
        return None

    # Already in set → replay
    return DetectionEvent(
        detection_id=f"det-{tx.event_id}",
        detection_type="replay_attack",
        severity="high",
        user_id=tx.user_id,
        transaction_id=tx.event_id,
        timestamp=datetime.now(timezone.utc),
    )
